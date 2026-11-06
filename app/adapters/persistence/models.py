"""Dialect-portable SQLAlchemy records for the production foundation."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class WorkspaceRecord(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "lifecycle IN ('provisioning', 'active', 'suspended', "
            "'deletion_pending', 'deleted')",
            name="workspace_lifecycle_valid",
        ),
        CheckConstraint(
            "length(budget_currency) = 3 "
            "AND substr(budget_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(budget_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(budget_currency, 3, 1) BETWEEN 'A' AND 'Z'",
            name="workspace_budget_currency_format",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    authorization_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The remaining three columns are the control-plane authorization
    # projection (spec §3.10): entitlement_version and suspended gate the
    # delegated identity middleware, and projection_version orders snapshot
    # application monotonically so a replayed/stale snapshot is a no-op.
    entitlement_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    budget_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    is_platform_workspace: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )


class ProductRecord(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_products_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "product_profile_entry_id",
            name="uq_products_workspace_profile",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    selling_points: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    audience: Mapped[str] = mapped_column(Text, nullable=False, default="")
    brand_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ref_images: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    economics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    product_profile_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Whether drafting cites the latest competitor snapshot of the plan's
    #: market (spec §7.2; written by C-23, read by C-24, shown by C-32).
    #: Deferred, and defaulted by the server only: product reads and inserts
    #: never name it, so they keep working while the Release service is
    #: still adding the column (it migrates in parallel with the API, #283).
    competitor_snapshot_in_drafts: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true(), deferred=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DimensionSchemaRecord(Base):
    __tablename__ = "dimension_schema_versions"
    __table_args__ = (
        UniqueConstraint("product_id", "version", name="uq_dimension_schema_product_version"),
        UniqueConstraint(
            "workspace_id", "id", name="uq_dimension_schema_versions_workspace_id_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_dimension_schema_versions_workspace_product_products",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    style_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DimensionRecord(Base):
    __tablename__ = "dimensions"
    __table_args__ = (UniqueConstraint("schema_id", "position", name="uq_dimension_schema_position"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_id: Mapped[str] = mapped_column(
        ForeignKey("dimension_schema_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stable_dimension_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class DimensionValueRecord(Base):
    __tablename__ = "dimension_values"
    __table_args__ = (UniqueConstraint("dimension_id", "position", name="uq_dimension_value_position"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dimension_id: Mapped[str] = mapped_column(ForeignKey("dimensions.id", ondelete="CASCADE"), nullable=False, index=True)
    stable_value_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_fragment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class PlanRecord(Base):
    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_plans_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_plans_workspace_product_products",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "schema_id"],
            ["dimension_schema_versions.workspace_id", "dimension_schema_versions.id"],
            name="fk_plans_workspace_schema_dimension_schema_versions",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    schema_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    cap: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_full: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PlanEntryRecord(Base):
    __tablename__ = "plan_entries"
    __table_args__ = (
        UniqueConstraint("plan_id", "combo_id", name="uq_plan_entry_combo"),
        UniqueConstraint("plan_id", "combo_fingerprint", name="uq_plan_entry_fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True)
    combo_id: Mapped[str] = mapped_column(String(64), nullable=False)
    combo_code: Mapped[str] = mapped_column(String(200), nullable=False)
    combo_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    values_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    tags_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class GenerationRunRecord(Base):
    __tablename__ = "generation_runs"
    __table_args__ = (
        CheckConstraint("est_cost >= 0", name="est_cost_nonnegative"),
        CheckConstraint("total >= 0 AND done >= 0 AND failed >= 0", name="run_counts_nonnegative"),
        UniqueConstraint(
            "workspace_id", "id", name="uq_generation_runs_workspace_id_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_generation_runs_workspace_product_products",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "plan_id"],
            ["plans.workspace_id", "plans.id"],
            name="fk_generation_runs_workspace_plan_plans",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "id", "execution_mode",
            name="uq_generation_runs_workspace_id_mode",
        ),
        CheckConstraint(
            "submission_kind IN ('confirmed_preview', 'legacy_migrated')",
            name="ck_generation_run_submission_kind",
        ),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock', 'controlled_mock')",
            name="ck_generation_run_execution_mode",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "recipe_snapshot_id"],
            ["generation_recipe_snapshots.workspace_id",
             "generation_recipe_snapshots.id"],
            name="fk_generation_runs_workspace_recipe_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "creation_preview_confirmation_id"],
            ["resolution_preview_confirmations.workspace_id",
             "resolution_preview_confirmations.id"],
            name="fk_generation_runs_workspace_confirmation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "creation_operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_generation_runs_workspace_creation_operation",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(20), nullable=False)
    image_size: Mapped[str] = mapped_column(String(20), nullable=False)
    variants: Mapped[int] = mapped_column(Integer, nullable=False)
    use_ref: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    est_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    recipe_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    creation_preview_confirmation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    creation_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submission_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="legacy_migrated")
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="mock")


class AssetRecord(Base):
    __tablename__ = "assets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_assets_workspace_product_products",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_assets_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["generation_runs.workspace_id", "generation_runs.id"],
            name="fk_assets_workspace_run_generation_runs",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "reused_from_asset_id"],
            ["assets.workspace_id", "assets.id"],
            name="fk_assets_workspace_reused_from_assets",
            ondelete="RESTRICT",
        ),
        # An external creative (Issue #168) is made outside MarketAI and has
        # no GenerationRun; every generated asset keeps its Run.
        CheckConstraint(
            "source IN ('generation', 'external')", name="ck_assets_source",
        ),
        CheckConstraint(
            "(source = 'external' AND run_id IS NULL) "
            "OR (source = 'generation' AND run_id IS NOT NULL)",
            name="ck_assets_source_run",
        ),
    )

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="generation", server_default="generation"
    )
    plan_entry_id: Mapped[str | None] = mapped_column(ForeignKey("plan_entries.id", ondelete="RESTRICT"), nullable=True)
    # The Worker copies RunItem.combination_fingerprint into both fields
    # (app.generation.worker._persist_output); see migration ze1f2a3b4c5d.
    combo_id: Mapped[str] = mapped_column(Text, nullable=False)
    combo_code: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reused_from_asset_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    error_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index(
    "ix_assets_success_fingerprint",
    AssetRecord.workspace_id,
    AssetRecord.fingerprint,
    AssetRecord.status,
)


class AssetVersionRecord(Base):
    __tablename__ = "asset_versions"
    __table_args__ = (
        UniqueConstraint("asset_id", "content_sha256", name="uq_asset_version_content"),
        UniqueConstraint(
            "workspace_id", "id", name="uq_asset_versions_workspace_id_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["assets.workspace_id", "assets.id"],
            name="fk_asset_versions_workspace_asset_assets",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "parent_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_asset_versions_workspace_parent_version_asset_versions",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_asset_versions_workspace_truth_pkg_truth_packages",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            # 'legacy' is the migration's server_default backfill value for
            # rows that predate the Generation -> Assets M1 columns (#21).
            # 'reuse' matches the vocabulary already established by
            # RunItemAssetVersionBindingRecord.kind ('produced'/'reused') and
            # the Creative Library reuse contract (ADR 0007).
            # 'localized' is a Localization output AssetVersion (Issue #24,
            # spec §1.11): it always carries truth_package_id and
            # parent_version_id, enforced below.
            "origin_kind IN ('generation', 'upload', 'reuse', 'legacy', 'localized', 'edit')",
            name="ck_asset_version_origin_kind",
        ),
        CheckConstraint(
            "execution_mode IS NULL OR execution_mode IN ('byok', 'sandbox', 'mock', 'controlled_mock')",
            name="ck_asset_version_execution_mode",
        ),
        UniqueConstraint(
            "workspace_id", "id", "truth_package_id", "parent_version_id",
            name="uq_av_workspace_truth_parent",
        ),
        CheckConstraint(
            "origin_kind != 'localized' OR "
            "(truth_package_id IS NOT NULL AND parent_version_id IS NOT NULL)",
            name="ck_av_localized_requires_truth_parent",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    parent_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(20), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    truth_package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    producing_run_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="legacy")
    execution_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    requested_seed: Mapped[str | None] = mapped_column(String(128), nullable=True)
    effective_seed: Mapped[str | None] = mapped_column(String(128), nullable=True)
    seed_unavailable_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    truth_dependency_expected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    truth_dependency_policy_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    truth_dependency_evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    truth_dependency_evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Committed Blob Ref (Issue #49, spec §7.4's `CommittedBlobRef`):
    # the exact committed key/version this AssetVersion's bytes live at.
    # Both nullable -- legacy AssetVersions (pre-#49) carry only the older
    # `object_key` column above; `resolve_asset_blob_ref` raises
    # `asset_blob_unavailable` when these are null (no fallback).
    committed_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    committed_storage_version_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Gallery/tile thumbnail (Issue #159): a 512px-long-edge JPEG derived
    # from the committed original by `app.generation.thumbnails` and
    # committed under its own content-addressed key with its own verified
    # checksum. All nullable -- a derivative is a best-effort cache of the
    # original (the original stays the only truth): legacy rows predate it
    # (`app.generation.thumbnail_backfill` fills them in), and a decode or
    # storage failure leaves them null rather than failing the RunItem.
    # Callers fall back to the original whenever any of the ref pair is null.
    thumbnail_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    thumbnail_storage_version_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    thumbnail_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    thumbnail_byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    thumbnail_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Reference lineage (Issue #167, v9 §3.1 "素材必须能反查……引用来源"): which
    # bound ``reference_asset`` versions the producing call actually sent
    # (primary first), the recipe's reference decision and the confirmed
    # must-match ids -- ``creative_recipe.recipe_lineage``. Written once at
    # insert by the BYOK evidence recorder; null for Mock output and rows
    # that predate it. Version ids and hashes only, never storage keys.
    reference_sources_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class QualityReviewRecord(Base):
    __tablename__ = "quality_reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_quality_reviews_workspace_product_products",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["assets.workspace_id", "assets.id"],
            name="fk_quality_reviews_workspace_asset_assets",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_quality_reviews_workspace_id_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "supersedes_review_id"],
            ["quality_reviews.workspace_id", "quality_reviews.id"],
            name="fk_quality_reviews_supersedes_review_id_quality_reviews",
        ),
        UniqueConstraint(
            "supersedes_review_id",
            name="uq_quality_reviews_supersedes",
        ),
        CheckConstraint(
            "review_value IS NULL OR review_value IN ('approved', 'rejected', 'needs_changes', 'cleared')",
            name="ck_quality_review_review_value",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="human")
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False, default="local-user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    asset_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    legacy_unscoped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    legacy_asset_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    review_value: Mapped[str | None] = mapped_column(String(32), nullable=True)
    supersedes_review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class VerdictDecisionRecord(Base):
    """Append-only compatibility record until ExperimentEntry decisions land."""

    __tablename__ = "verdict_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_verdict_decisions_workspace_product_products",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["assets.workspace_id", "assets.id"],
            name="fk_verdict_decisions_workspace_asset_assets",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    context_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="legacy")
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False, default="local-user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_lane_ready", "type", "state", "created_at", "id"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_job_idempotency"),
        CheckConstraint("attempt >= 0 AND max_attempts >= 1", name="job_attempts_valid"),
        CheckConstraint(
            "execution_contract_version = 0 OR "
            "(operation_id IS NOT NULL AND entitlement_version_at_enqueue IS NOT NULL)",
            name="jobs_v1_requires_operation_and_entitlement_version",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_jobs_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_jobs_workspace_operation_operation_records",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    initiator_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    initiator_role: Mapped[str] = mapped_column(String(20), nullable=False)
    request_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    trace_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    authorization_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    resource_id: Mapped[str] = mapped_column(String(100), nullable=False, default="", index=True)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    safe_to_retry_after_crash: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    error_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entitlement_version_at_enqueue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CostLedgerRecord(Base):
    __tablename__ = "cost_ledger"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="cost_amount_nonnegative"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["generation_runs.workspace_id", "generation_runs.id"],
            name="fk_cost_ledger_workspace_run_generation_runs",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["jobs.workspace_id", "jobs.id"],
            name="fk_cost_ledger_workspace_job_jobs",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class ImportBatchRecord(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source_hash", name="uq_import_batch_workspace_source"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    source_root: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    report_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEventRecord(Base):
    """Append-only, tenant-scoped attribution for domain command evidence."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    initiator_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    initiator_role: Mapped[str] = mapped_column(String(20), nullable=False)
    executor: Mapped[str] = mapped_column(String(100), nullable=False)
    request_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    trace_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    authorization_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(100), nullable=False, default="", index=True)
    details_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )


class MetaAccountRecord(Base):
    """Meta account projection plus the start of the continuous observation window."""

    __tablename__ = "meta_accounts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "ad_account_id", name="uq_meta_account_workspace_ad"),
        UniqueConstraint(
            "workspace_id", "id", name="uq_meta_accounts_workspace_id_id"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    timezone_name: Mapped[str] = mapped_column(String(100), nullable=False, default="UTC")
    api_version: Mapped[str] = mapped_column(String(20), nullable=False)
    observation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observation_contract_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MetaSyncRunRecord(Base):
    __tablename__ = "meta_sync_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_meta_sync_idempotency"),
        UniqueConstraint(
            "workspace_id", "id", name="uq_meta_sync_runs_workspace_id_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["meta_accounts.workspace_id", "meta_accounts.id"],
            name="fk_meta_sync_runs_workspace_account_meta_accounts",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    date_start: Mapped[date] = mapped_column(Date, nullable=False)
    date_stop: Mapped[date] = mapped_column(Date, nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    api_version: Mapped[str] = mapped_column(String(20), nullable=False)
    account_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="")
    account_timezone: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    error_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MetaIngestionRecord(Base):
    __tablename__ = "meta_ingestions"
    __table_args__ = (UniqueConstraint("sync_run_id", "kind", name="uq_meta_ingestion_run_kind"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sync_run_id: Mapped[str] = mapped_column(ForeignKey("meta_sync_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    next_after_cursor: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetch_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    error_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MetaIngestionPageRecord(Base):
    __tablename__ = "meta_ingestion_pages"
    __table_args__ = (
        UniqueConstraint("ingestion_id", "page_number", name="uq_meta_ingestion_page_number"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ingestion_id: Mapped[str] = mapped_column(ForeignKey("meta_ingestions.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_after_cursor: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    response_after_cursor: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    response_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    response_meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AdDailyMetricRecord(Base):
    __tablename__ = "ad_daily_metrics"
    __table_args__ = (
        UniqueConstraint("account_id", "ad_id", "metric_date", "contract_hash", name="uq_ad_metric_contract_day"),
        CheckConstraint("spend >= 0 AND impressions >= 0 AND clicks >= 0", name="ad_metric_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("meta_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    sync_run_id: Mapped[str] = mapped_column(ForeignKey("meta_sync_runs.id", ondelete="CASCADE"), nullable=False)
    ad_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    ad_name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    adset_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    adset_name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    spend: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"))
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reach: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ctr: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    cpc: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    cpm: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    purchases: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"))
    purchase_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, default=Decimal("0"))
    purchase_roas: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    frequency: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    attribution_setting: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MetaEntityStatusEventRecord(Base):
    __tablename__ = "meta_entity_status_events"
    __table_args__ = (
        UniqueConstraint("sync_run_id", "entity_type", "entity_id", name="uq_meta_status_run_entity"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("meta_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    sync_run_id: Mapped[str] = mapped_column(ForeignKey("meta_sync_runs.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    configured_status: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    effective_status: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MetaAdBindingRecord(Base):
    __tablename__ = "meta_ad_bindings"
    __table_args__ = (
        UniqueConstraint("account_id", "ad_id", name="uq_meta_binding_account_ad"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("meta_accounts.id", ondelete="CASCADE"), nullable=False)
    ad_id: Mapped[str] = mapped_column(String(100), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(ForeignKey("asset_versions.id", ondelete="RESTRICT"), nullable=False)
    experiment_entry_id: Mapped[str | None] = mapped_column(
        ForeignKey("experiment_entries.id", ondelete="RESTRICT"), nullable=True
    )
    launch_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("launch_operations.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    campaign_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    adset_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    creative_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="launch_saga")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LaunchPreviewRecord(Base):
    __tablename__ = "launch_previews"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id", name="uq_launch_previews_workspace_id_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_launch_previews_workspace_experiment_experiments",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_launch_previews_workspace_asset_version_asset_versions",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_entry_id"],
            ["experiment_entries.workspace_id", "experiment_entries.id"],
            name="fk_launch_previews_workspace_entry_experiment_entries",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_entry_id: Mapped[str] = mapped_column(ForeignKey("experiment_entries.id", ondelete="RESTRICT"), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    preview_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LaunchConfirmationRecord(Base):
    __tablename__ = "launch_confirmations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    preview_id: Mapped[str] = mapped_column(ForeignKey("launch_previews.id", ondelete="CASCADE"), nullable=False, index=True)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LaunchOperationRecord(Base):
    __tablename__ = "launch_operations"
    __table_args__ = (
        UniqueConstraint("preview_id", name="uq_launch_operation_preview"),
        UniqueConstraint(
            "workspace_id", "id", name="uq_launch_operations_workspace_id_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "preview_id"],
            ["launch_previews.workspace_id", "launch_previews.id"],
            name="fk_launch_operations_workspace_preview_launch_previews",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_launch_operations_workspace_experiment_experiments",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    preview_id: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_id: Mapped[str] = mapped_column(ForeignKey("launch_confirmations.id", ondelete="RESTRICT"), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    current_step: Mapped[str] = mapped_column(String(64), nullable=False, default="planned")
    marker: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    error_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LaunchStepRecord(Base):
    __tablename__ = "launch_steps"
    __table_args__ = (UniqueConstraint("operation_id", "step_key", name="uq_launch_step_operation_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("launch_operations.id", ondelete="CASCADE"), nullable=False, index=True)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    remote_object_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    remote_object_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    response_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    error_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LaunchAuditRecord(Base):
    __tablename__ = "launch_audit_log"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["launch_operations.workspace_id", "launch_operations.id"],
            name="fk_launch_audit_log_workspace_operation_launch_operations",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    details_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MetaSyncScheduleRecord(Base):
    __tablename__ = "meta_sync_schedules"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_meta_schedule_account"),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["meta_accounts.workspace_id", "meta_accounts.id"],
            name="fk_meta_sync_schedules_workspace_account_meta_accounts",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timezone_name: Mapped[str] = mapped_column(String(100), nullable=False, default="UTC")
    local_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentRecord(Base):
    """Mutable lifecycle projection; configuration becomes immutable at freeze."""

    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="experiment_revision_positive"),
        CheckConstraint(
            "record_schema_version IS NULL OR record_schema_version = 2",
            name="ck_experiments_schema_version",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_experiments_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_experiments_workspace_product_products",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    metric_contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    frozen_snapshot_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    record_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentEntryRecord(Base):
    """A draft candidate. Removing one sets ``removed_at`` (#284): the domain
    database grants no DELETE, so the row stays and every read skips it. Only
    live entries hold a position or an asset version."""

    __tablename__ = "experiment_entries"
    __table_args__ = (
        Index(
            "uq_experiment_entry_position", "experiment_id", "position", unique=True,
            sqlite_where=text("removed_at IS NULL"), postgresql_where=text("removed_at IS NULL"),
        ),
        Index(
            "uq_experiment_entry_asset_version", "experiment_id", "asset_version_id", unique=True,
            sqlite_where=text("removed_at IS NULL"), postgresql_where=text("removed_at IS NULL"),
        ),
        UniqueConstraint("workspace_id", "id", name="uq_experiment_entries_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_experiment_entries_workspace_experiment_experiments",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_experiment_entries_workspace_asset_version_asset_versions",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    marketing_vars_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentAdBindingRecord(Base):
    __tablename__ = "experiment_ad_bindings"
    __table_args__ = (
        UniqueConstraint("experiment_entry_id", name="uq_experiment_binding_entry"),
        UniqueConstraint("ad_account_id", "ad_id", name="uq_experiment_binding_account_ad"),
        UniqueConstraint("workspace_id", "id", name="uq_experiment_ad_bindings_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_entry_id"],
            ["experiment_entries.workspace_id", "experiment_entries.id"],
            name="fk_experiment_ad_bindings_workspace_entry_experiment_entries",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_entry_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ad_account_id: Mapped[str] = mapped_column(String(100), nullable=False)
    ad_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual_read_only")
    record_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentStateEventRecord(Base):
    __tablename__ = "experiment_state_events"
    __table_args__ = (
        UniqueConstraint("experiment_id", "revision", name="uq_experiment_state_revision"),
        UniqueConstraint("workspace_id", "id", name="uq_experiment_state_events_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_experiment_state_events_workspace_experiment_experiments",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False, default="local-user")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class JudgementRecord(Base):
    __tablename__ = "judgements"
    __table_args__ = (
        UniqueConstraint("experiment_id", "snapshot_hash", "algorithm_version", name="uq_judgement_snapshot_algorithm"),
        UniqueConstraint("workspace_id", "id", name="uq_judgements_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "experiment_id",
            "experiment_version_id",
            "id",
            name="uq_judgements_workspace_exp_ver_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_judgements_workspace_experiment_experiments",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    record_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentDecisionRecord(Base):
    """Append-only projection-friendly decisions emitted by a judgement."""

    __tablename__ = "experiment_decisions"
    __table_args__ = (
        UniqueConstraint("judgement_id", "experiment_entry_id", name="uq_decision_judgement_entry"),
        UniqueConstraint("workspace_id", "id", name="uq_experiment_decisions_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "id",
            "experiment_id",
            "experiment_version_id",
            "experiment_version_candidate_id",
            name="uq_ed_workspace_id_exp_ver_cand",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_experiment_decisions_workspace_experiment_experiments",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "judgement_id"],
            ["judgements.workspace_id", "judgements.id"],
            name="fk_experiment_decisions_workspace_judgement_judgements",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_entry_id"],
            ["experiment_entries.workspace_id", "experiment_entries.id"],
            name="fk_experiment_decisions_workspace_entry_experiment_entries",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    judgement_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    experiment_version_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    metric: Mapped[str] = mapped_column(String(20), nullable=False)
    estimate: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    interval_low: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    interval_high: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    reason_codes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    context_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentVersionRecord(Base):
    """Immutable frozen snapshot — one per Experiment, created at freeze."""

    __tablename__ = "experiment_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_experiment_versions_workspace_id"),
        UniqueConstraint("experiment_id", name="uq_experiment_versions_experiment_id"),
        UniqueConstraint(
            "workspace_id", "experiment_id", "id",
            name="uq_experiment_versions_workspace_exp_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_experiment_versions_workspace_experiment_experiments",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_ev_ws_product_ver_library_versions",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_experiment_versions_workspace_offer_version_library_versions",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "attribution_click_window_days BETWEEN 1 AND 90",
            name="ck_ev_click_window",
        ),
        CheckConstraint(
            "attribution_view_window_days BETWEEN 1 AND 90",
            name="ck_ev_view_window",
        ),
        CheckConstraint("daily_budget_amount > 0", name="ck_ev_daily_budget_positive"),
        CheckConstraint(
            "total_budget_cap >= daily_budget_amount",
            name="ck_ev_total_budget_gte_daily",
        ),
        CheckConstraint(
            "schedule_end_at > schedule_start_at",
            name="ck_ev_schedule_order",
        ),
        Index("ix_experiment_versions_frozen_snapshot_hash", "frozen_snapshot_hash"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(String(40), nullable=False)
    frozen_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    frozen_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    placement: Mapped[str] = mapped_column(String(40), nullable=False)
    landing_page_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    audience_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    audience_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    daily_budget_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    total_budget_cap: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    budget_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    schedule_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    attribution_click_window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    attribution_view_window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    statistical_plan_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    statistical_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    marketing_vars_schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidates_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    concept_resolutions_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentVersionCandidateRecord(Base):
    """Immutable post-freeze identity per candidate."""

    __tablename__ = "experiment_version_candidates"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_evc_workspace_id"),
        UniqueConstraint(
            "workspace_id", "experiment_version_id", "id",
            name="uq_evc_workspace_version_id",
        ),
        UniqueConstraint(
            "experiment_version_id", "position",
            name="uq_evc_version_position",
        ),
        UniqueConstraint(
            "experiment_version_id", "asset_version_id",
            name="uq_evc_version_asset",
        ),
        UniqueConstraint(
            "workspace_id", "experiment_version_id", "id", "asset_version_id",
            name="uq_evc_workspace_version_id_asset",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id"],
            ["experiment_versions.workspace_id", "experiment_versions.id"],
            name="fk_evc_workspace_version_experiment_versions",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_evc_workspace_asset_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source_kind IN ('generation', 'upload', 'reuse', 'manual')",
            name="ck_evc_source_kind",
        ),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_evc_execution_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_run_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    marketing_vars_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    marketing_vars_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    eligibility_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    eligibility_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    concepts_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentVersionConceptRecord(Base):
    """Multi-concept per candidate; open-ended dimension key."""

    __tablename__ = "experiment_version_candidate_concepts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_evcc_workspace_id"),
        UniqueConstraint(
            "experiment_version_candidate_id", "dimension_key",
            name="uq_evcc_candidate_dimension",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_evcc_ws",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id", "experiment_version_candidate_id"],
            [
                "experiment_version_candidates.workspace_id",
                "experiment_version_candidates.experiment_version_id",
                "experiment_version_candidates.id",
            ],
            name="fk_evcc_workspace_version_candidate_evc",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["concept_key_id"],
            ["dimension_concept_keys.id"],
            name="fk_evcc_concept_key",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "concept_key_scope IN ('workspace', 'platform')",
            name="ck_evcc_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_version_candidate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dimension_key: Mapped[str] = mapped_column(Text, nullable=False)
    stable_dimension_signature: Mapped[str] = mapped_column(Text, nullable=False)
    concept_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    concept_key_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    concept_key_name: Mapped[str] = mapped_column(String(300), nullable=False)
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    duplicate_group_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentVersionResolutionRecord(Base):
    """Immutable concept-conflict resolution per duplicate group."""

    __tablename__ = "experiment_version_concept_resolutions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_evcr_workspace_id"),
        UniqueConstraint(
            "experiment_version_id", "duplicate_group_hash",
            name="uq_evcr_version_group",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_evcr_ws",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id"],
            ["experiment_versions.workspace_id", "experiment_versions.id"],
            name="fk_evcr_ws_ver_ev",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["concept_key_id"],
            ["dimension_concept_keys.id"],
            name="fk_evcr_concept_key",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "action IN ('allow_controlled_duplicate', 'exclude_source_candidate', "
            "'remap_before_freeze')",
            name="ck_evcr_action",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    duplicate_group_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension_key: Mapped[str] = mapped_column(String(200), nullable=False)
    concept_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_candidate_fingerprints_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DecisionProposalRecord(Base):
    """Immutable proposal header — schema-2 only."""

    __tablename__ = "decision_proposals"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_dp_workspace_id"),
        UniqueConstraint(
            "workspace_id", "id", "experiment_id", "experiment_version_id", "judgement_id",
            name="uq_dp_full_lineage",
        ),
        UniqueConstraint(
            "workspace_id", "judgement_id", "eligibility_context_hash",
            name="uq_dp_judgement_eligibility",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_dp_workspace_experiment_experiments",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id",
                "experiment_id",
                "experiment_version_id",
                "judgement_id",
            ],
            [
                "judgements.workspace_id",
                "judgements.experiment_id",
                "judgements.experiment_version_id",
                "judgements.id",
            ],
            name="fk_dp_workspace_exp_ver_judgement",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id", "experiment_version_id"],
            [
                "experiment_versions.workspace_id",
                "experiment_versions.experiment_id",
                "experiment_versions.id",
            ],
            name="fk_dp_workspace_experiment_version_experiment_versions",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    judgement_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    eligibility_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_entries_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DecisionProposalStateEventRecord(Base):
    """Append-only lifecycle events for proposals."""

    __tablename__ = "decision_proposal_state_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_dpse_workspace_id"),
        UniqueConstraint(
            "workspace_id", "proposal_id", "sequence",
            name="uq_dpse_proposal_sequence",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["decision_proposals.workspace_id", "decision_proposals.id"],
            name="fk_dpse_workspace_proposal_decision_proposals",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('created', 'consumed', 'invalidated', 'superseded')",
            name="ck_dpse_event_type",
        ),
        CheckConstraint("sequence >= 1", name="ck_dpse_sequence_positive"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DecisionConfirmationRecord(Base):
    """Gate between Proposal and material Decision."""

    __tablename__ = "decision_confirmations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_dc_workspace_id"),
        UniqueConstraint(
            "workspace_id", "id", "proposal_id",
            name="uq_dc_workspace_id_proposal",
        ),
        UniqueConstraint(
            "workspace_id", "proposal_id",
            name="uq_dc_workspace_proposal",
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key_hash",
            name="uq_dc_workspace_idempotency",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["decision_proposals.workspace_id", "decision_proposals.id"],
            name="fk_dc_workspace_proposal_decision_proposals",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_proposal_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    authorization_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    authorization_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentEligibilityEventRecord(Base):
    """Append-only eligibility events with idempotent deduplication."""

    __tablename__ = "experiment_eligibility_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_eee_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id", "experiment_version_id"],
            [
                "experiment_versions.workspace_id",
                "experiment_versions.experiment_id",
                "experiment_versions.id",
            ],
            name="fk_eee_workspace_experiment_version_experiment_versions",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "upstream_event_type IN ('review_rejected', 'qc_failed_blocking', "
            "'product_version_withdrawn', 'offer_version_withdrawn', "
            "'product_version_eligibility_changed', 'offer_version_eligibility_changed', "
            "'stale_truth_detected', 'review_re_approved', 'qc_remediated')",
            name="ck_eee_upstream_event_type",
        ),
        CheckConstraint(
            "upstream_resource_type IN ('quality_review', 'asset_qc_attempt', "
            "'library_version', 'truth_package', 'market_asset_binding')",
            name="ck_eee_upstream_resource_type",
        ),
        CheckConstraint(
            "previous_effective_eligibility IN ('eligible', 'ineligible', 'unknown')",
            name="ck_eee_prev_eligibility",
        ),
        CheckConstraint(
            "current_effective_eligibility IN ('eligible', 'ineligible')",
            name="ck_eee_curr_eligibility",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    upstream_event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    upstream_resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    upstream_resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    upstream_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_effective_eligibility: Mapped[str] = mapped_column(String(20), nullable=False)
    current_effective_eligibility: Mapped[str] = mapped_column(String(20), nullable=False)
    reversible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentEligibilityProjectionRecord(Base):
    """Rebuildable current-eligibility projection."""

    __tablename__ = "experiment_eligibility_projections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_eep_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id", "experiment_version_id"],
            [
                "experiment_versions.workspace_id",
                "experiment_versions.experiment_id",
                "experiment_versions.id",
            ],
            name="fk_eep_workspace_experiment_version_experiment_versions",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "level IN ('version', 'candidate')",
            name="ck_eep_level",
        ),
        CheckConstraint(
            "current_eligibility IN ('eligible', 'ineligible')",
            name="ck_eep_eligibility",
        ),
        CheckConstraint(
            "(level = 'version' AND candidate_id IS NULL) OR "
            "(level = 'candidate' AND candidate_id IS NOT NULL)",
            name="ck_eep_level_candidate",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_eligibility: Mapped[str] = mapped_column(String(20), nullable=False)
    irreversible_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    head_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ContextualVerdictProjectionRecord(Base):
    """Rebuildable current-verdict read projection."""

    __tablename__ = "contextual_verdict_projections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_cvp_workspace_id"),
        UniqueConstraint(
            "workspace_id", "experiment_id", "asset_version_id", "offer_version_id",
            "market", "placement", "landing_page_evidence_hash",
            name="uq_cvp_current_key",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_cvp_workspace_experiment_experiments",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id"],
            ["experiment_versions.workspace_id", "experiment_versions.id"],
            name="fk_cvp_workspace_version_experiment_versions",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "verdict IN ('win', 'learn', 'lose')",
            name="ck_cvp_verdict",
        ),
        Index(
            "ix_cvp_asset_confirmed",
            "workspace_id",
            "asset_version_id",
            text("confirmed_at DESC"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    metric: Mapped[str] = mapped_column(String(20), nullable=False)
    estimate: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    placement: Mapped[str] = mapped_column(String(40), nullable=False)
    landing_page_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentTimelineHeadRecord(Base):
    """Per-experiment monotonic sequence head (lock class 7)."""

    __tablename__ = "experiment_timeline_heads"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "experiment_id",
            name="uq_eth_workspace_experiment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_eth_workspace_experiment_experiments",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ExperimentTimelineEventRecord(Base):
    """Append-only event log with per-experiment monotonic sequence."""

    __tablename__ = "experiment_timeline_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_ete_workspace_id_id"),
        UniqueConstraint(
            "workspace_id", "experiment_id", "timeline_sequence",
            name="uq_ete_sequence",
        ),
        UniqueConstraint(
            "workspace_id", "source_resource_type", "source_resource_id", "event_type",
            name="uq_ete_source_dedup",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_ete_workspace_experiment_experiments",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeline_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentNotificationOutboxRecord(Base):
    """Append-only notification intent — no delivery columns."""

    __tablename__ = "experiment_notification_outbox"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "timeline_event_id", "notification_type",
            name="uq_eno_event_type",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "timeline_event_id"],
            ["experiment_timeline_events.workspace_id", "experiment_timeline_events.id"],
            name="fk_eno_workspace_event_experiment_timeline_events",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "payload_schema_version IN (1)",
            name="ck_eno_schema_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeline_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentNextRoundRecord(Base):
    """Immutable next-round action linking Decision to GenerationDraft."""

    __tablename__ = "experiment_next_round_records"
    __table_args__ = (
        UniqueConstraint("generation_draft_id", name="uq_enrr_draft"),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_enrr_workspace_experiment_experiments",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "generation_draft_id"],
            ["generation_drafts.workspace_id", "generation_drafts.id"],
            name="fk_enrr_workspace_draft",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id",
                "decision_id",
                "experiment_id",
                "experiment_version_id",
                "source_candidate_id",
            ],
            [
                "experiment_decisions.workspace_id",
                "experiment_decisions.id",
                "experiment_decisions.experiment_id",
                "experiment_decisions.experiment_version_id",
                "experiment_decisions.experiment_version_candidate_id",
            ],
            name="fk_enrr_decision_lineage",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id",
                "experiment_version_id",
                "source_candidate_id",
                "source_asset_version_id",
            ],
            [
                "experiment_version_candidates.workspace_id",
                "experiment_version_candidates.experiment_version_id",
                "experiment_version_candidates.id",
                "experiment_version_candidates.asset_version_id",
            ],
            name="fk_enrr_candidate_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "qc_attempt_id", "source_asset_version_id"],
            [
                "asset_qc_attempts.workspace_id",
                "asset_qc_attempts.id",
                "asset_qc_attempts.asset_version_id",
            ],
            name="fk_enrr_qc_attempt",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "intent_kind IN ('same_recipe', 'new_seed', 'edit_prompt', 'quality_feedback')",
            name="ck_enrr_intent_kind",
        ),
        CheckConstraint(
            "(intent_kind = 'quality_feedback' AND qc_attempt_id IS NOT NULL) OR "
            "(intent_kind != 'quality_feedback' AND qc_attempt_id IS NULL)",
            name="ck_enrr_qc_intent",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_draft_id: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    qc_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExperimentPatternCandidateRecord(Base):
    """Immutable pattern candidate from confirmed material Decision."""

    __tablename__ = "experiment_pattern_candidates"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "decision_id",
            name="uq_epc_workspace_decision",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name="fk_epc_workspace_experiment_experiments",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id",
                "decision_id",
                "experiment_id",
                "experiment_version_id",
                "candidate_id",
            ],
            [
                "experiment_decisions.workspace_id",
                "experiment_decisions.id",
                "experiment_decisions.experiment_id",
                "experiment_decisions.experiment_version_id",
                "experiment_decisions.experiment_version_candidate_id",
            ],
            name="fk_epc_decision_lineage",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_version_id", "candidate_id"],
            [
                "experiment_version_candidates.workspace_id",
                "experiment_version_candidates.experiment_version_id",
                "experiment_version_candidates.id",
            ],
            name="fk_epc_workspace_version_candidate",
            ondelete="RESTRICT",
        ),
        CheckConstraint("concept_count > 0", name="ck_epc_concept_count"),
        CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_epc_mode",
        ),
        CheckConstraint(
            "verdict IN ('win', 'learn', 'lose')",
            name="ck_epc_verdict",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_concepts_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    concept_count: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    pattern_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    pattern_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class OperationRecord(Base):
    """Persistent, tenant-scoped representation of an async domain command.

    Spec §5.1: the user-facing Operation is a database record, not a
    transient in-memory model. ``state`` is a projection over the
    append-only :class:`OperationStateEventRecord` history below -- every
    transition updates both in the same flush, never one without the other.
    """

    __tablename__ = "operation_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id", name="uq_operation_records_workspace_id_id"
        ),
        UniqueConstraint(
            "workspace_id", "semantic_key_hash", name="uq_operation_semantic_key"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    initiator_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    command_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_resource_id: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    semantic_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    admission_contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class OperationStateEventRecord(Base):
    """Append-only history of one Operation's state transitions (spec §5.1-5.2).

    Never updated or deleted: ``operation_records.state`` is the current
    projection, this table is the immutable evidence it was built from.
    """

    __tablename__ = "operation_state_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_operation_state_events_workspace_operation_operation_records",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class UploadIntentRecord(Base):
    """Three-phase upload lifecycle: staged, verified, committed (spec §7.9-7.10).

    ``state`` is a CAS-guarded projection: the Worker acquires ``verifying``
    with a conditional ``UPDATE ... WHERE state = 'pending'`` (see
    ``app.platform.upload_intents.acquire_verification_lease``), so only one
    Worker ever holds the lease. ``committed_blob_ref_key`` /
    ``committed_blob_ref_version`` are populated exactly once, on the
    ``verifying -> finalized`` transition -- this table *is* the "database
    unique constraint on upload_intent_id" the spec calls for, since the
    committed reference lives on the intent row itself rather than a
    separate table.
    """

    __tablename__ = "upload_intent_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id", name="uq_upload_intent_records_workspace_id_id"
        ),
        UniqueConstraint(
            "staging_key", name="uq_upload_intent_records_staging_key"
        ),
        CheckConstraint(
            "state IN ('pending', 'verifying', 'finalized', 'rejected', 'expired')",
            name="upload_intent_state_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    initiator_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    authorization_epoch_at_issue: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    staging_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    filename_original: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    content_length_min: Mapped[int] = mapped_column(Integer, nullable=False)
    content_length_max: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256_b64: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    verification_lease_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejection_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    committed_blob_ref_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    committed_blob_ref_version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DownloadIntentRecord(Base):
    """Immutable per-click download audit record (spec §7.12).

    Never updated after creation -- idempotent replay of
    ``create_download_intent`` re-issues a signed capability against the
    same ``committed_blob_ref_key``/``committed_blob_ref_version`` without
    touching this row (see ``app.platform.idempotency.create_or_replay_intent``'s
    ``replay_fn``).
    """

    __tablename__ = "download_intent_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id", name="uq_download_intent_records_workspace_id_id"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    initiator_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(100), nullable=False)
    committed_blob_ref_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    committed_blob_ref_version: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class IdempotencyRecord(Base):
    """Pending-reservation idempotency ledger (spec §5.3-5.4).

    Two idempotency patterns share this table: async commands (202 +
    Operation) bind ``operation_id``; sync resource creation (201, e.g.
    upload/download intents) binds ``response_resource_type`` +
    ``response_resource_id`` instead -- exactly one of the two is ever set.
    The ``state`` column distinguishes a claimed-but-unresolved reservation
    (``pending``) from a resolved outcome (``committed``); the CHECK
    constraints below enforce that split at the database level so a partial
    write can never leave outcome data on an uncommitted reservation, or a
    committed record with no outcome at all.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint(
            "actor_id != '' AND command_type != ''",
            name="idempotency_actor_binding",
        ),
        CheckConstraint(
            "state IN ('pending', 'committed')",
            name="idempotency_state_valid",
        ),
        CheckConstraint(
            "state != 'pending' OR ("
            "response_status IS NULL AND operation_id IS NULL "
            "AND response_resource_type IS NULL AND response_resource_id IS NULL"
            ")",
            name="idempotency_pending_has_no_outcome",
        ),
        CheckConstraint(
            "state != 'committed' OR (response_status IS NOT NULL AND ("
            "(operation_id IS NOT NULL AND response_resource_type IS NULL "
            "AND response_resource_id IS NULL) OR "
            "(operation_id IS NULL AND response_resource_type IS NOT NULL "
            "AND response_resource_id IS NOT NULL)"
            "))",
            name="idempotency_committed_has_outcome",
        ),
        # Composite, not a plain operation_id -> operation_records.id FK: the
        # tenant-safe-foreign-keys invariant (see f0a1b2c3d4e5, mirrored by
        # PlanRecord/AssetRecord above and enforced by
        # tests/test_postgres_rls.py::test_workspace_owned_foreign_keys_are_tenant_composite)
        # requires every cross-table reference between two workspace-owned
        # tables to bind workspace_id alongside the id, via
        # operation_records' own uq_operation_records_workspace_id_id --
        # otherwise a naked operation_id both admits a cross-tenant relation
        # and reveals whether a guessed id exists, and PostgreSQL's FK
        # triggers are not filtered by RLS.
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_idempotency_records_workspace_operation_operation_records",
        ),
        Index("idx_idempotency_expires", "expires_at"),
    )

    #: Composite primary key (key_hash, workspace_id) -- spec §5.3: the same
    #: raw Idempotency-Key is scoped per Workspace, never global.
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command_type: Mapped[str] = mapped_column(String(100), nullable=False)
    request_fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Creative Library core (#19)
#
# 契约：docs/product/creative-library-ownership-and-reuse-contract.md
# ADR 0007：稳定身份 + 乐观锁 Draft + 不可变 Published Version。
# ---------------------------------------------------------------------------


class LibraryEntryRecord(Base):
    """创意库条目：Workspace 内的稳定身份（§3.1）。名称不是业务身份。"""

    __tablename__ = "library_entries"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_library_entries_workspace_id"),
        CheckConstraint("lifecycle IN ('active','archived')", name="lifecycle_enum"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    current_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index(
    "ix_library_entries_scope",
    LibraryEntryRecord.workspace_id,
    LibraryEntryRecord.entry_type,
    LibraryEntryRecord.lifecycle,
)


class LibraryDraftRecord(Base):
    """可编辑的下一版本（§3.1）。每个条目至多一个 Draft，写入必须带 revision。"""

    __tablename__ = "library_drafts"
    __table_args__ = (
        UniqueConstraint("entry_id", name="uq_library_draft_entry"),
        ForeignKeyConstraint(
            ["workspace_id", "entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_library_drafts_workspace_entry",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "base_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_library_drafts_workspace_base_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="draft_revision_positive"),
        CheckConstraint("state IN ('editing','validation_failed','ready_to_publish')", name="draft_state_enum"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    base_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_command: Mapped[str] = mapped_column(String(20), nullable=False, default="create")
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    content_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="editing")
    validation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LibraryVersionRecord(Base):
    """一次原子发布得到的不可变内容（§3.1、§3.4）。没有 PUT/PATCH。"""

    __tablename__ = "library_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_library_versions_workspace_id"),
        UniqueConstraint("entry_id", "version_no", name="uq_library_version_no"),
        ForeignKeyConstraint(
            ["workspace_id", "entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_library_versions_workspace_entry",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint("lifecycle_revision >= 1", name="lifecycle_revision_positive"),
        CheckConstraint("state IN ('effective','superseded','withdrawn')", name="version_state_enum"),
        CheckConstraint("eligibility IN ('eligible','warning','blocked')", name="version_eligibility_enum"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lifecycle_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="effective", index=True)
    eligibility: Mapped[str] = mapped_column(String(20), nullable=False, default="eligible")
    validation_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    base_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_command: Mapped[str] = mapped_column(String(20), nullable=False, default="create")
    published_by: Mapped[str] = mapped_column(String(100), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_by: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    withdraw_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")


# 一个条目最多只能有一个 effective 版本。这是数据库层的不变量，不依赖服务层
# 先读后写：并发发布中第二个事务会在这里被拒绝，而不是产生两个当前版本。
Index(
    "uq_library_version_single_effective",
    LibraryVersionRecord.entry_id,
    unique=True,
    sqlite_where=text("state = 'effective'"),
    postgresql_where=text("state = 'effective'"),
)


class LibraryDependencyRecord(Base):
    """DependencyIndex（§14）：发布时冻结的版本→版本引用边。

    当前只覆盖创意库内部的版本→版本引用。契约 §14 还要求索引 Generation
    Draft/Run、AssetVersion、Experiment、LocalizationBatch、TruthPackage、
    MarketAssetBinding 和 Launch；那些对象目前引用旧的 products 表而不是
    LibraryVersion，接入属于 #20/#21 及后续 ticket。
    """

    __tablename__ = "library_dependencies"
    __table_args__ = (
        UniqueConstraint("from_version_id", "to_version_id", "relation", name="uq_library_dependency_edge"),
        ForeignKeyConstraint(
            ["workspace_id", "from_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_library_dependencies_workspace_from_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "to_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_library_dependencies_workspace_to_version",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(60), nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ReferenceObjectRecord(Base):
    """参考文件的物理对象：quarantine 原件 + 受控派生（§7.2）。

    ``workspace_id`` 参与唯一约束：同一 Workspace 内相同文件复用对象存储，
    跨 Workspace 不得出现可观察的 hash 去重。
    """

    __tablename__ = "reference_objects"
    __table_args__ = (
        UniqueConstraint("workspace_id", "content_sha256", name="uq_reference_object_workspace_content"),
        CheckConstraint("state IN ('quarantined','rejected','released')", name="reference_object_state_enum"),
        CheckConstraint("byte_size > 0", name="reference_object_byte_size_positive"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 原件位置：生产路径是 BlobStore 的 committed key（``committed/{ws}/reference_asset/...``），
    # 加上该对象的 storage revision；本地同步上传路径仍是 LIBRARY_OBJECT_DIR 下的绝对路径，
    # 此时 ``original_storage_version_id`` 为空（zg2b3c4d5e6f）。
    original_object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    original_storage_version_id: Mapped[str] = mapped_column(
        String(256), nullable=False, default="", server_default=""
    )
    declared_mime: Mapped[str] = mapped_column(String(100), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="quarantined", index=True)
    # 被拒绝的原件从对象存储清除，只留下检查证据与内容 hash。
    # server_default 必须与 df25fa1c9683 里的一致，否则将来打开
    # compare_server_default 会立刻报出漂移。
    original_purged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    checks_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    execution_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="mock")
    had_exif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    derivative_object_key: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    # 受控派生写回 BlobStore 后的 storage revision（本地目录路径为空），签名下载
    # 与 Provider 读取都用它和 key 一起定位同一不可变对象。
    derivative_storage_version_id: Mapped[str] = mapped_column(
        String(256), nullable=False, default="", server_default=""
    )
    derivative_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    derivative_byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    derivative_mime: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    derivative_width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    derivative_height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 服务端 promote 为该对象创建的 reference_asset 条目；同一对象再次 promote
    # 回到同一条目/版本，而不是发布第二份参考。
    promoted_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


#: ``product_intakes.status`` (Issue #161). In-flight: queued -> fetching ->
#: extracting -> preparing_images; terminal for the job: ready,
#: fetch_failed, extraction_failed, failed; ready -> confirmed by the user.
PRODUCT_INTAKE_STATUSES = (
    "queued", "fetching", "extracting", "preparing_images",
    "ready", "fetch_failed", "extraction_failed", "failed", "confirmed",
)


class ProductIntakeRecord(Base):
    """One "paste a link / images / text -> product card" intake (Issue #161).

    The Worker fills ``evidence_json`` (per-step outcome and duration, page
    digests -- never the page itself), ``extraction_json`` (the model's
    answer, or the raw output when it failed validation), ``images_json``
    (ranked candidates and their reference-gate results) and ``card_json``
    (the AI draft the user edits). ``confirmation_json`` and the
    ``product_*`` ids record what the user's confirmation published.
    """

    __tablename__ = "product_intakes"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in PRODUCT_INTAKE_STATUSES) + ")",
            name="product_intake_status",
        ),
        CheckConstraint(
            "execution_mode IN ('mock', 'byok')", name="product_intake_execution_mode",
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key_hash", name="uq_product_intake_idempotency",
        ),
        Index(
            "ix_product_intakes_fingerprint", "workspace_id", "input_fingerprint", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(32), nullable=True)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_intent_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    inputs_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    extraction_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    images_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    card_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confirmation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    product_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    initiator_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


EXTERNAL_CREATIVE_STATUSES = (
    "queued", "gating", "preparing_asset", "tagging",
    "ready", "tagging_failed", "confirmed", "rejected", "failed",
)
EXTERNAL_CREATIVE_MADE_BY = ("self", "designer", "agency", "creator", "ai_tool", "other")


class ExternalCreativeRecord(Base):
    """One image made outside MarketAI, uploaded to join tests (Issue #168).

    Front-flow contract §5.2: a Lovart / designer / UGC image becomes a
    candidate. The row tracks the upload (``upload_intent_id`` and the
    committed original), the uploader's rights declaration, the reference
    gate verdict (``reference_object_id``), the AssetVersion it became
    (``asset_id`` / ``asset_version_id``, origin ``upload``), the dimension
    vocabulary frozen at admission and the governed ``llm_chat`` tagging
    suggestion. The user's confirmed tags live in the append-only
    :class:`ExternalCreativeTagConfirmationRecord`.
    """

    __tablename__ = "external_creatives"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in EXTERNAL_CREATIVE_STATUSES) + ")",
            name="ck_external_creatives_status",
        ),
        CheckConstraint(
            "execution_mode IN ('mock', 'byok')", name="ck_external_creatives_execution_mode",
        ),
        CheckConstraint(
            "made_by IN (" + ", ".join(f"'{s}'" for s in EXTERNAL_CREATIVE_MADE_BY) + ")",
            name="ck_external_creatives_made_by",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_external_creatives_workspace_id_id"),
        UniqueConstraint(
            "workspace_id", "product_id", "upload_sha256", name="uq_external_creatives_content",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_external_creatives_workspace_product",
            ondelete="RESTRICT",
        ),
        Index("ix_external_creatives_product", "workspace_id", "product_id", "created_at"),
        Index("ix_external_creatives_batch", "workspace_id", "batch_key_hash"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(32), nullable=True)
    upload_intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    upload_key: Mapped[str] = mapped_column(String(512), nullable=False)
    upload_storage_version_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    upload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    upload_mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    upload_byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    made_by: Mapped[str] = mapped_column(String(16), nullable=False)
    rights_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    vocabulary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    vocabulary_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    suggestion_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reference_object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    asset_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    batch_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    initiator_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExternalCreativeTagConfirmationRecord(Base):
    """One immutable confirmation of an external creative's tags (Issue #168).

    The user's reviewed tags (``tags_json``, each with the AI suggestion it
    accepted or overrode), the concept identity written for them
    (``concepts_json`` -- the same ``dck_`` keys and ``<template>:<dimension>``
    keys a generated combination carries, plus its ``stable_dimension_
    signature``) and the learning placement (``plan_id`` / ``direction_key``
    / ``cells_json``). Append-only: editing the tags appends ``revision + 1``
    superseding the previous row; experiments freeze the concepts they read
    and the learnings snapshot hashes the placement, so history is never
    rewritten.
    """

    __tablename__ = "external_creative_tag_confirmations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_ectc_workspace_id_id"),
        UniqueConstraint("external_creative_id", "revision", name="uq_ectc_creative_revision"),
        ForeignKeyConstraint(
            ["workspace_id", "external_creative_id"],
            ["external_creatives.workspace_id", "external_creatives.id"],
            name="fk_ectc_workspace_creative",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_ectc_workspace_asset_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="ck_ectc_revision"),
        Index("ix_ectc_asset_version", "workspace_id", "asset_version_id", "revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vocabulary_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tags_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    concepts_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    stable_dimension_signature: Mapped[str] = mapped_column(Text, nullable=False, default="")
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cells_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


AD_TEST_PLAN_STATUSES = ("drafting", "ready", "failed")
AD_TEST_PLAN_GOALS = ("explore", "optimize")
AD_TEST_PLAN_SCALES = ("sample", "small", "full")
AD_TEST_PLAN_RUN_STAGES = ("sample", "full")
AD_TEST_PLAN_RUN_STATUSES = ("materializing", "submitted", "failed")


class AdTestPlanRecord(Base):
    """One "set the ad test" plan for a product (Issue #163, contract §3.2).

    Provider-neutral by construction (contract §5.1): ``recipe_json`` holds
    the frozen product facts, the factor catalog (values with positive /
    negative English fragments) and plan constraints -- never a provider,
    model or prompt. A Run binds those at run time from the Workspace's
    generation defaults. ``revision`` is the If-Match revision ``revise``
    advances; the directions live in :class:`AdTestPlanDirectionRecord`.
    """

    __tablename__ = "test_plans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_test_plans_workspace_id_id"),
        UniqueConstraint(
            "workspace_id", "idempotency_key_hash", name="uq_test_plans_idempotency",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in AD_TEST_PLAN_STATUSES) + ")",
            name="ck_test_plans_status",
        ),
        CheckConstraint(
            "goal IN (" + ", ".join(f"'{s}'" for s in AD_TEST_PLAN_GOALS) + ")",
            name="ck_test_plans_goal",
        ),
        CheckConstraint(
            "scale IN (" + ", ".join(f"'{s}'" for s in AD_TEST_PLAN_SCALES) + ")",
            name="ck_test_plans_scale",
        ),
        CheckConstraint("revision >= 1", name="ck_test_plans_revision"),
        Index("ix_test_plans_product", "workspace_id", "product_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    goal: Mapped[str] = mapped_column(String(16), nullable=False)
    audience_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    audience_card_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scale: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recipe_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    seed_asset_version_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    template_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdTestPlanDirectionRecord(Base):
    """One creative direction of a test plan: what the card shows the user.

    ``factor_values_json`` maps factor key -> value ids from the plan's
    factor catalog; ``combination_signatures_json`` is the direction's
    planner space (after plan constraints) as plan-local signatures.
    ``removed`` is a soft delete (the user dropped the direction) so runs
    that already used it still resolve; ``user_added`` marks the user's own
    ideas. No provider, model or prompt lives here (contract §5.1).
    """

    __tablename__ = "test_plan_directions"
    __table_args__ = (
        UniqueConstraint("plan_id", "direction_key", name="uq_test_plan_direction_key"),
        UniqueConstraint("plan_id", "ordinal", name="uq_test_plan_direction_ordinal"),
        ForeignKeyConstraint(
            ["workspace_id", "plan_id"],
            ["test_plans.workspace_id", "test_plans.id"],
            name="fk_test_plan_directions_workspace_plan",
            ondelete="CASCADE",
        ),
        CheckConstraint("ordinal >= 0", name="ck_test_plan_direction_ordinal"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    direction_key: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    what_it_tests: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scene_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    copy_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    factor_values_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    fragments_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    combination_signatures_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    user_added: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AdTestPlanRunRecord(Base):
    """The one generation Run a plan stage materialised into.

    Unique per (plan, stage): replaying ``POST /test-plans/{id}/runs`` with
    the same stage resumes or returns this row's Run. It freezes the plan
    revision, the generation defaults it ran with and the selection
    (selected / reused signatures, counts, maximum cost), and links the AI
    dimension template version, generation draft, preview and Run.
    """

    __tablename__ = "test_plan_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "plan_id", "stage", name="uq_test_plan_run_stage"),
        ForeignKeyConstraint(
            ["workspace_id", "plan_id"],
            ["test_plans.workspace_id", "test_plans.id"],
            name="fk_test_plan_runs_workspace_plan",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "stage IN (" + ", ".join(f"'{s}'" for s in AD_TEST_PLAN_RUN_STAGES) + ")",
            name="ck_test_plan_runs_stage",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in AD_TEST_PLAN_RUN_STATUSES) + ")",
            name="ck_test_plan_runs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    target_scale: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    defaults_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    selection_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    template_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    template_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class GenerationDefaultsRecord(Base):
    """Workspace setting「生成默认值」(front-flow contract §4).

    Absent until an Owner saves one; until then the effective defaults are
    computed (BYOK OpenAI GPT-Image when a generation key is bound, else
    controlled Mock). Test-plan runs read the effective defaults at run time
    and freeze them into the Run's recipe.
    """

    __tablename__ = "generation_defaults"
    __table_args__ = (
        CheckConstraint(
            "execution_mode IN ('mock', 'byok')", name="ck_generation_defaults_mode",
        ),
        CheckConstraint(
            "variants_sample >= 1 AND variants_small >= 1 AND variants_full >= 1",
            name="ck_generation_defaults_variants",
        ),
        CheckConstraint("revision >= 1", name="ck_generation_defaults_revision"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(16), nullable=False)
    variants_sample: Mapped[int] = mapped_column(Integer, nullable=False)
    variants_small: Mapped[int] = mapped_column(Integer, nullable=False)
    variants_full: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


# ---------------------------------------------------------------------------
# Results loop (Issue #166): ad-level results -> dimension-value learnings
# ---------------------------------------------------------------------------

RESULT_IMPORT_SOURCES = ("manual", "csv", "meta_sync")
RESULT_IMPORT_STATUSES = ("previewed", "committed")
#: Evidence partitions (CLAUDE.md "Controlled Mock ... cannot mix with real
#: evidence"): rows on BYOK assets with customer metrics are ``production``;
#: anything touching a controlled-Mock asset or Mock Meta data is ``mock``;
#: Meta Sandbox data is ``sandbox``. Snapshots never mix partitions.
EVIDENCE_PARTITIONS = ("production", "sandbox", "mock")


class ResultImportRecord(Base):
    """One results import: parsed rows, their match preview, then a commit.

    ``rows_json`` keeps every parsed row with its match outcome (asset,
    combination, reason) so the preview is reproducible and auditable;
    committing appends the accepted rows to :class:`ResultObservationRecord`.
    ``fingerprint`` hashes the normalized rows: the same file or rows for
    the same product resolve to the same import (never imported twice).
    """

    __tablename__ = "result_imports"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_result_imports_workspace_id_id"),
        UniqueConstraint(
            "workspace_id", "product_id", "fingerprint", name="uq_result_imports_fingerprint",
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key_hash", name="uq_result_imports_idempotency",
        ),
        CheckConstraint(
            "source IN (" + ", ".join(f"'{s}'" for s in RESULT_IMPORT_SOURCES) + ")",
            name="ck_result_imports_source",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in RESULT_IMPORT_STATUSES) + ")",
            name="ck_result_imports_status",
        ),
        Index("ix_result_imports_product", "workspace_id", "product_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    file_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    upload_intent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    rows_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    committed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResultObservationRecord(Base):
    """One committed ad-level result row: append-only evidence.

    Attributed to one AssetVersion of the product (and optionally the Meta
    ``ad_id`` that ran it) over ``[date_start, date_end]``. A later import
    with the same (asset, ad, date range) supersedes an earlier one in the
    projection -- the earlier row is never rewritten. ``conversions`` /
    ``spend`` are NULL when the source did not report them.
    """

    __tablename__ = "result_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "import_id"],
            ["result_imports.workspace_id", "result_imports.id"],
            name="fk_result_observations_workspace_import",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "source IN (" + ", ".join(f"'{s}'" for s in RESULT_IMPORT_SOURCES) + ")",
            name="ck_result_observations_source",
        ),
        CheckConstraint(
            "evidence_mode IN (" + ", ".join(f"'{s}'" for s in EVIDENCE_PARTITIONS) + ")",
            name="ck_result_observations_mode",
        ),
        CheckConstraint(
            "impressions >= 0 AND clicks >= 0 AND clicks <= impressions "
            "AND (spend IS NULL OR spend >= 0) AND (conversions IS NULL OR conversions >= 0) "
            "AND (revenue IS NULL OR revenue >= 0) AND date_end >= date_start",
            name="ck_result_observations_values",
        ),
        Index("ix_result_observations_product", "workspace_id", "product_id", "evidence_mode"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    import_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    date_start: Mapped[date] = mapped_column(Date, nullable=False)
    date_end: Mapped[date] = mapped_column(Date, nullable=False)
    impressions: Mapped[int] = mapped_column(BigInteger, nullable=False)
    clicks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spend: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    conversions: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LearningSnapshotRecord(Base):
    """One immutable learnings computation for a product and partition.

    ``input_hash`` covers the algorithm version, thresholds and every input
    the computation read (effective observations, attribution, plan
    catalogs); recomputing the same inputs yields the same ``result_json``,
    so an unchanged input reuses the latest snapshot instead of writing a
    new one. Rows are never updated (no UPDATE grant).
    """

    __tablename__ = "learning_snapshots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_learning_snapshots_workspace_id_id"),
        CheckConstraint(
            "evidence_mode IN (" + ", ".join(f"'{s}'" for s in EVIDENCE_PARTITIONS) + ")",
            name="ck_learning_snapshots_mode",
        ),
        Index("ix_learning_snapshots_product", "workspace_id", "product_id", "evidence_mode", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_import_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ReferenceBindingRecord(Base):
    """把 ReferenceAssetVersion 绑定到产品/人物/参考组版本（§7.1）。

    文件 hash 相同不合并不同 Binding。
    """

    __tablename__ = "reference_bindings"
    __table_args__ = (
        UniqueConstraint(
            "owner_version_id", "reference_version_id", "purpose", "viewpoint", name="uq_reference_binding_slot"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "owner_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_reference_bindings_workspace_owner_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "reference_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_reference_bindings_workspace_reference_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("priority >= 0", name="reference_binding_priority_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reference_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(100), nullable=False)
    viewpoint: Mapped[str] = mapped_column(String(100), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    conditions_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LibraryEventRecord(Base):
    """追加式创意库事件（§3.2、§14）。当前状态是它的投影，不覆盖历史。"""

    __tablename__ = "library_events"
    __table_args__ = (
        UniqueConstraint("entry_id", "sequence", name="uq_library_event_sequence"),
        ForeignKeyConstraint(
            ["workspace_id", "entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_library_events_workspace_entry",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    from_state: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    to_state: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    initiator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    executor: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LibraryAuditRecord(Base):
    """创意库命令审计：双 Actor 归因（initiator + executor），匹配 #16 平台审计。"""

    __tablename__ = "library_audit"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    command: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    initiator_id: Mapped[str] = mapped_column(String(100), nullable=False)
    initiator_role: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    executor: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    authorization_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    detail_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LibraryIdempotencyRecord(Base):
    """Request-level idempotency for library commands (§17).

    Gate 0.2 extensions: contract schema version, command-identity hash,
    lifecycle state, response kind, updated/reconciled timestamps.
    """

    __tablename__ = "library_idempotency"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_library_idempotency_key"),
        UniqueConstraint(
            "workspace_id", "command_identity_hash",
            name="uq_library_idempotency_command_identity",
        ),
        CheckConstraint(
            "lifecycle_state IN ('pending', 'terminal')",
            name="ck_library_idempotency_lifecycle_state",
        ),
        CheckConstraint(
            "response_kind IN ('success', 'problem', 'not_applied', 'pending')",
            name="ck_library_idempotency_response_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    command: Mapped[str] = mapped_column(String(40), nullable=False)
    initiator_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    response_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    contract_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    command_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    lifecycle_state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    response_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Gate 0.2: command impact projections, entry contexts, step-up JTI (#47)
# ---------------------------------------------------------------------------


class LibraryCommandImpactRecord(Base):
    """Server-computed impact projection for Publish, Withdraw, or Archive.

    The browser requests an impact before executing a destructive command.
    The matching command verifies impact_id + impact_hash atomically.
    """

    __tablename__ = "library_command_impacts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "impact_hash", name="uq_library_command_impact_hash"),
        CheckConstraint(
            "command_kind IN ('publish', 'withdraw', 'archive')",
            name="ck_library_command_impact_kind",
        ),
        CheckConstraint(
            "consumption_state IN ('available', 'consumed', 'expired')",
            name="impact_consumption",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_entry_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    impact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    consumption_state: Mapped[str] = mapped_column(String(20), nullable=False, default="available")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LibraryEntryContextRecord(Base):
    """Contextual navigation association for internal resource entries.

    Not an ownership record — an entry may appear in multiple legal contexts.
    The browser uses this to resolve parent/resource navigation paths.
    """

    __tablename__ = "library_entry_contexts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "root_entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_library_entry_context_root",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "resource_entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_library_entry_context_resource",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "parent_entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_library_entry_context_parent",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "workspace_id", "root_entry_id", "resource_entry_id", "relation_kind",
            name="uq_library_entry_context",
        ),
        CheckConstraint(
            "root_entry_id != resource_entry_id",
            name="ck_library_entry_context_no_self_ref",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    root_entry_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_entry_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parent_entry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relation_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    creation_evidence: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class StepUpAssertionRecord(Base):
    """One-use MFA step-up assertion JTI consumption.

    Scoped to workspace and actor. Stores only a hash of the JTI,
    never the assertion itself.

    No longer written: MFA is checked only at login (ADR 0015). The table
    and its rows stay as audit evidence of the earlier step-up checks.
    """

    __tablename__ = "library_stepup_assertions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "jti_hash",
            name="uq_library_stepup_jti",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    jti_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Creative Library extensions: templates, prompt policy, datasources (#20)
#
# 契约：docs/product/creative-library-ownership-and-reuse-contract.md
# ---------------------------------------------------------------------------


class DimensionConceptKeyRecord(Base):
    """维度模板跨产品复用的稳定概念键（platform 或 workspace 作用域）。"""

    __tablename__ = "dimension_concept_keys"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'workspace' AND workspace_id IS NOT NULL) "
            "OR (scope = 'platform' AND workspace_id IS NULL)",
            name="ck_concept_key_scope_workspace",
        ),
        CheckConstraint(
            "scope IN ('workspace', 'platform')",
            name="ck_concept_key_scope_enum",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class PromptBaselineRecord(Base):
    """平台级 Prompt 规则基线：单一 effective 版本，追加式发布/撤回。"""

    __tablename__ = "prompt_baselines"
    __table_args__ = (
        CheckConstraint("version_no >= 1", name="ck_prompt_baseline_version_positive"),
        CheckConstraint(
            "state IN ('effective', 'superseded', 'withdrawn')",
            name="ck_prompt_baseline_state_enum",
        ),
        UniqueConstraint("version_no", name="uq_prompt_baseline_version_no"),
        Index(
            "ix_prompt_baseline_one_effective",
            "state",
            unique=True,
            sqlite_where=text("state = 'effective'"),
            postgresql_where=text("state = 'effective'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    published_by: Mapped[str] = mapped_column(String(100), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdraw_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    withdrawn_by: Mapped[str] = mapped_column(String(100), nullable=False, default="")


class DatasourceCredentialBindingRecord(Base):
    """DataSource 条目到 CredentialVersion 的当前绑定（单一 active revision）。"""

    __tablename__ = "datasource_credential_bindings"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_dcb_revision_positive"),
        CheckConstraint(
            "state IN ('active', 'superseded')",
            name="ck_dcb_state_enum",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "data_source_entry_id"],
            ["library_entries.workspace_id", "library_entries.id"],
            name="fk_dcb_workspace_entry",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "credential_version_id"],
            ["credential_versions.workspace_id", "credential_versions.id"],
            name="fk_dcb_workspace_credential",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "data_source_entry_id", "revision", name="uq_dcb_entry_revision"
        ),
        Index(
            "ix_dcb_one_active",
            "data_source_entry_id", "state",
            unique=True,
            sqlite_where=text("state = 'active'"),
            postgresql_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_source_entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ResolutionPreviewRecord(Base):
    """一次 GenerationDraft 真值解析的冻结快照，供 Apply 提交前预览与核对。"""

    __tablename__ = "resolution_previews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_resolution_previews_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_resolution_previews_workspace_id"
        ),
        # Two typed purposes only (design spec "Modified tables >
        # resolution_previews"): 'initial_generation' freezes a
        # GenerationDraft; 'run_recovery' freezes a retry/resume target
        # selection against an existing GenerationRun.
        CheckConstraint(
            "purpose IN ('initial_generation', 'run_recovery')",
            name="ck_resolution_preview_purpose",
        ),
        CheckConstraint(
            "recovery_command_kind IS NULL OR recovery_command_kind IN "
            "('retry_failed_items', 'resume_paused_items')",
            name="ck_resolution_preview_recovery_cmd_kind",
        ),
        CheckConstraint(
            "purpose != 'initial_generation' OR ("
            "generation_draft_id IS NOT NULL AND draft_revision IS NOT NULL "
            "AND recovery_run_id IS NULL AND recovery_command_kind IS NULL"
            ")",
            name="ck_resolution_preview_initial_fields",
        ),
        CheckConstraint(
            "purpose != 'run_recovery' OR ("
            "recovery_run_id IS NOT NULL AND recovery_command_kind IS NOT NULL "
            "AND recovery_max_additional_cost IS NOT NULL "
            "AND recovery_cost_currency IS NOT NULL "
            "AND generation_draft_id IS NULL AND draft_revision IS NULL"
            ")",
            name="ck_resolution_preview_recovery_fields",
        ),
        CheckConstraint(
            "recovery_cost_currency IS NULL OR ("
            "length(recovery_cost_currency) = 3 "
            "AND substr(recovery_cost_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(recovery_cost_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(recovery_cost_currency, 3, 1) BETWEEN 'A' AND 'Z'"
            ")",
            name="ck_resolution_preview_recovery_ccy_fmt",
        ),
        CheckConstraint(
            "recovery_max_additional_cost IS NULL OR "
            "recovery_max_additional_cost >= 0",
            name="ck_resolution_preview_recovery_cost_nn",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    generation_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    snapshot_schema_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="20.1"
    )
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recipe_input_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    execution_mode: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    valid_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidation_reason: Mapped[str] = mapped_column(
        String(300), nullable=False, default=""
    )
    submitted_run_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purpose: Mapped[str] = mapped_column(String(40), nullable=False, default="initial_generation")
    recovery_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recovery_command_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    recovery_target_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    recovery_max_additional_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    recovery_cost_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)


# ---------------------------------------------------------------------------
# Localization — Platform-scoped tables (Issue #24, spec §1.2)
# ---------------------------------------------------------------------------
#
# Market Policy is a platform-global catalog (v9 Beta ships nine fixed
# markets), not Workspace data -- these three tables deliberately carry no
# `workspace_id` and sit outside the tenant boundary that `tenant_session`
# and RLS enforce for domain data elsewhere in this module.


class MarketPolicyRecord(Base):
    __tablename__ = "market_policies"
    __table_args__ = (
        UniqueConstraint("market_id", name="uq_mp_market_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_id: Mapped[str] = mapped_column(String(10), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class MarketPolicyVersionRecord(Base):
    __tablename__ = "market_policy_versions"
    __table_args__ = (
        UniqueConstraint("market_policy_id", "version_number",
                         name="uq_mpv_policy_version"),
        UniqueConstraint("id", "market_policy_id",
                         name="uq_mpv_id_policy"),
        CheckConstraint(
            "version_state IN ('effective', 'superseded')",
            name="ck_mpv_version_state",
        ),
        CheckConstraint(
            "shipping_address_granularity IN ('country', 'region', 'postal_code')",
            name="ck_mpv_shipping_granularity",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_policy_id: Mapped[str] = mapped_column(
        ForeignKey("market_policies.id"), nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_state: Mapped[str] = mapped_column(String(20), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    pricing_context_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    digit_format_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    base_tone: Mapped[str] = mapped_column(String(40), nullable=False)
    reserved_words_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    restricted_claim_types_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    requires_local_copy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    shipping_address_granularity: Mapped[str] = mapped_column(String(20), nullable=False)
    max_fact_staleness_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    qc_rules_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class MarketPolicyVersionEventRecord(Base):
    __tablename__ = "market_policy_version_events"
    __table_args__ = (
        UniqueConstraint("market_policy_id", "sequence",
                         name="uq_mpve_policy_sequence"),
        UniqueConstraint("market_policy_version_id", "event_type",
                         name="uq_mpve_version_event_type"),
        UniqueConstraint("deduplication_key",
                         name="uq_mpve_dedup_key"),
        UniqueConstraint("id", "market_policy_version_id",
                         name="uq_mpve_id_version"),
        ForeignKeyConstraint(
            ["market_policy_version_id", "market_policy_id"],
            ["market_policy_versions.id", "market_policy_versions.market_policy_id"],
            name="fk_mpve_version_policy_mpv",
        ),
        CheckConstraint(
            "event_type IN ('published', 'superseded')",
            name="ck_mpve_event_type",
        ),
        CheckConstraint(
            "current_state IN ('effective', 'superseded')",
            name="ck_mpve_current_state",
        ),
        CheckConstraint("sequence >= 1", name="ck_mpve_sequence_positive"),
        CheckConstraint(
            "(event_type = 'published' AND previous_state IS NULL "
            "AND current_state = 'effective') OR "
            "(event_type = 'superseded' AND previous_state = 'effective' "
            "AND current_state = 'superseded')",
            name="ck_mpve_state_consistency",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_policy_id: Mapped[str] = mapped_column(
        ForeignKey("market_policies.id"), nullable=False,
    )
    market_policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("market_policy_versions.id"), nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_state: Mapped[str] = mapped_column(String(20), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# Localization — Truth resolution tables (Issue #24, spec §1.3)
# ---------------------------------------------------------------------------
#
# Every table here is workspace-scoped and immutable (PostgreSQL append-only
# triggers land with the migration in Task 7; this module only declares the
# ORM shape). Composite foreign keys always carry `workspace_id` so a row in
# Workspace A can never reference a row in Workspace B, even without RLS.
# `truth_package_markets` re-exposes progressively wider "parent-carrying"
# unique keys -- e.g. `(workspace_id, id, truth_package_id)` -- purely so that
# child tables can compose a single FK that simultaneously proves row
# identity and correct-parent membership, instead of two separate FKs that
# could independently point at mismatched parents.


class TruthPackageRecord(Base):
    __tablename__ = "truth_packages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_tp_workspace_id"),
        UniqueConstraint("workspace_id", "id", "truth_data_mode",
                         name="uq_tp_workspace_id_mode"),
        UniqueConstraint("workspace_id", "canonical_hash",
                         name="uq_tp_workspace_hash"),
        ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tp_product_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tp_offer_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_resolution_policy_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tp_truth_resolution_policy_version",
        ),
        CheckConstraint(
            "truth_data_mode IN ('real', 'mock')",
            name="ck_tp_truth_data_mode",
        ),
        CheckConstraint(
            "status IN ('full', 'degraded', 'failed')",
            name="ck_tp_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_resolution_policy_version_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    truth_data_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class TruthPackageMarketRecord(Base):
    __tablename__ = "truth_package_markets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_tpm_workspace_id"),
        UniqueConstraint("workspace_id", "truth_package_id", "market",
                         name="uq_tpm_workspace_package_market"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         name="uq_tpm_workspace_id_package"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "market_policy_version_id",
                         name="uq_tpm_workspace_id_package_mpv"),
        UniqueConstraint("workspace_id", "id", "truth_package_id", "market",
                         "market_policy_version_id",
                         name="uq_tpm_workspace_id_package_market_mpv"),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_tpm_workspace_package",
        ),
        CheckConstraint(
            "readiness IN ('ready', 'degraded', 'failed', 'unavailable')",
            name="ck_tpm_readiness",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("market_policy_versions.id"), nullable=False,
    )
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    readiness: Mapped[str] = mapped_column(String(20), nullable=False)
    readiness_detail_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class TruthSourceAttemptRecord(Base):
    __tablename__ = "truth_source_attempts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "truth_package_market_id", "fact_type",
            "scope_key", "priority_ordinal",
            name="uq_tsa_workspace_market_fact_scope_priority",
        ),
        UniqueConstraint(
            "workspace_id", "id", "truth_package_id", "truth_package_market_id",
            "fact_type", "scope_key",
            name="uq_tsa_workspace_id_package_market_fact_scope",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
            ],
            name="fk_tsa_market",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "data_source_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tsa_data_source_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "market_source_mapping_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tsa_market_source_mapping_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "manual_fact_set_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tsa_manual_fact_set_version",
        ),
        CheckConstraint(
            "truth_data_mode IN ('real', 'mock')",
            name="ck_tsa_truth_data_mode",
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'failed', 'skipped')",
            name="ck_tsa_outcome",
        ),
        CheckConstraint(
            "(outcome = 'failed' AND failure_code IS NOT NULL) OR "
            "(outcome != 'failed' AND failure_code IS NULL)",
            name="ck_tsa_failure_code",
        ),
        CheckConstraint(
            "(outcome = 'skipped' AND skip_code IS NOT NULL) OR "
            "(outcome != 'skipped' AND skip_code IS NULL)",
            name="ck_tsa_skip_code",
        ),
        CheckConstraint(
            "(outcome = 'accepted' AND evidence_hash IS NOT NULL) OR "
            "(outcome IN ('failed', 'skipped') AND evidence_hash IS NULL)",
            name="ck_tsa_evidence_hash",
        ),
        CheckConstraint(
            "(adapter_kind = 'manual' AND manual_fact_set_version_id IS NOT NULL "
            "AND truth_data_mode = 'real') OR "
            "(adapter_kind != 'manual' AND manual_fact_set_version_id IS NULL)",
            name="ck_tsa_manual_adapter",
        ),
        CheckConstraint(
            "(truth_data_mode = 'mock' AND mock_scenario IS NOT NULL "
            "AND mock_version IS NOT NULL) OR "
            "(truth_data_mode = 'real' AND mock_scenario IS NULL "
            "AND mock_version IS NULL)",
            name="ck_tsa_mock_fields",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False)
    priority_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    data_source_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_source_mapping_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    adapter_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(40), nullable=False)
    manual_fact_set_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    resolution_rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    truth_data_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_context_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    mock_scenario: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mock_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    skip_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class TruthFactRecord(Base):
    __tablename__ = "truth_facts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_tf_workspace_id"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         name="uq_tf_workspace_id_package"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "truth_package_market_id",
                         name="uq_tf_workspace_id_package_market"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "truth_package_market_id", "scope_key",
                         name="uq_tf_workspace_id_package_market_scope"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "truth_package_market_id", "fact_type", "scope_key",
                         name="uq_tf_workspace_id_package_market_type_scope"),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
            ],
            name="fk_tf_market",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id", "accepted_source_attempt_id", "truth_package_id",
                "truth_package_market_id", "fact_type", "scope_key",
            ],
            [
                "truth_source_attempts.workspace_id",
                "truth_source_attempts.id",
                "truth_source_attempts.truth_package_id",
                "truth_source_attempts.truth_package_market_id",
                "truth_source_attempts.fact_type",
                "truth_source_attempts.scope_key",
            ],
            name="fk_tf_accepted_attempt",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False)
    accepted_source_attempt_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_object_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    requested_context_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    mock_scenario: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mock_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    valid_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    source_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class TruthDerivedFactRecord(Base):
    __tablename__ = "truth_derived_facts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_tdf_workspace_id"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "truth_package_market_id",
                         name="uq_tdf_workspace_id_package_market"),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
            ],
            name="fk_tdf_market",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    derived_type: Mapped[str] = mapped_column(String(40), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False)
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    derivation_rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class TruthDerivedFactInputRecord(Base):
    __tablename__ = "truth_derived_fact_inputs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "derived_fact_id", "input_fact_id",
                         name="uq_tdfi_workspace_derived_input"),
        ForeignKeyConstraint(
            ["workspace_id", "derived_fact_id", "truth_package_id",
             "truth_package_market_id"],
            [
                "truth_derived_facts.workspace_id",
                "truth_derived_facts.id",
                "truth_derived_facts.truth_package_id",
                "truth_derived_facts.truth_package_market_id",
            ],
            name="fk_tdfi_derived_fact",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "input_fact_id", "truth_package_id",
             "truth_package_market_id"],
            [
                "truth_facts.workspace_id",
                "truth_facts.id",
                "truth_facts.truth_package_id",
                "truth_facts.truth_package_market_id",
            ],
            name="fk_tdfi_input_fact",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    derived_fact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fact_id: Mapped[str] = mapped_column(String(64), nullable=False)


class FactLibraryDependencyRecord(Base):
    __tablename__ = "fact_library_dependencies"
    __table_args__ = (
        Index(
            "ix_fld_fact_level_dep",
            "workspace_id", "truth_package_id", "truth_fact_id",
            "dependency_kind", "dependency_version_id",
            unique=True,
            sqlite_where=text("truth_fact_id IS NOT NULL"),
            postgresql_where=text("truth_fact_id IS NOT NULL"),
        ),
        Index(
            "ix_fld_package_level_dep",
            "workspace_id", "truth_package_id",
            "dependency_kind", "dependency_version_id",
            unique=True,
            sqlite_where=text("truth_fact_id IS NULL"),
            postgresql_where=text("truth_fact_id IS NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_fld_package",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_fact_id", "truth_package_id"],
            [
                "truth_facts.workspace_id",
                "truth_facts.id",
                "truth_facts.truth_package_id",
            ],
            name="fk_fld_fact",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "dependency_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_fld_dependency_version",
        ),
        CheckConstraint(
            "(truth_fact_id IS NULL AND dependency_kind IN "
            "('product_version', 'offer_version', "
            "'truth_resolution_policy_version')) OR "
            "(truth_fact_id IS NOT NULL AND dependency_kind IN "
            "('data_source_version', 'manual_fact_set_version', "
            "'market_source_mapping_version'))",
            name="ck_fld_dependency_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_fact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dependency_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    dependency_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class FactMarketPolicyDependencyRecord(Base):
    __tablename__ = "fact_market_policy_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "truth_package_id", "truth_package_market_id",
            "market_policy_version_id",
            name="uq_fmpd_workspace_package_market_mpv",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_fmpd_package",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id",
             "market_policy_version_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_fmpd_market_mpv",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("market_policy_versions.id"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# Localization -- Source claim tables (Issue #24, spec §1.4)
# ---------------------------------------------------------------------------
#
# Same conventions as the truth-resolution block above: every table is
# workspace-scoped, composite FKs always carry `workspace_id`, and the
# PostgreSQL append-only/immutable triggers land with the migration in
# Task 7 -- this module only declares the ORM shape.


class SourceClaimInventoryRecord(Base):
    __tablename__ = "source_claim_inventories"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_sci_workspace_id"),
        UniqueConstraint("workspace_id", "id", "asset_version_id",
                         name="uq_sci_workspace_id_asset_version"),
        UniqueConstraint("workspace_id", "id", "asset_version_id",
                         "analysis_execution_mode",
                         name="uq_sci_workspace_id_asset_version_mode"),
        UniqueConstraint("workspace_id", "asset_version_id",
                         "analysis_recipe_hash", "version_number",
                         name="uq_sci_workspace_asset_recipe_version"),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_sci_asset_version",
        ),
        CheckConstraint(
            "analysis_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_sci_analysis_execution_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_recipe_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    claims_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    analysis_execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(40), nullable=False)
    mock_scenario: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mock_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class SourceClaimConfirmationRecord(Base):
    __tablename__ = "source_claim_confirmations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_scc_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "inventory_id"],
            ["source_claim_inventories.workspace_id",
             "source_claim_inventories.id"],
            name="fk_scc_inventory",
        ),
        CheckConstraint(
            "confirmation_kind IN ('accepted', 'rejected', 'needs_review')",
            name="ck_scc_confirmation_kind",
        ),
        CheckConstraint(
            "role IN ('owner', 'member')",
            name="ck_scc_role",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    inventory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_index: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmation_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizationClaimDecisionSetRecord(Base):
    __tablename__ = "localization_claim_decision_sets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lcds_workspace_id"),
        UniqueConstraint(
            "workspace_id", "id", "inventory_id", "truth_package_id",
            "truth_package_market_id", "target_market", "market_policy_version_id",
            name="uq_lcds_workspace_id_context",
        ),
        UniqueConstraint(
            "workspace_id", "id", "source_asset_version_id", "inventory_id",
            "truth_package_id", "truth_package_market_id", "target_market",
            "market_policy_version_id",
            name="uq_lcds_workspace_id_source_context",
        ),
        UniqueConstraint("workspace_id", "canonical_hash",
                         name="uq_lcds_workspace_hash"),
        ForeignKeyConstraint(
            ["workspace_id", "origin_batch_id", "truth_package_id"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
            ],
            name="fk_lcds_origin_batch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "inventory_id", "source_asset_version_id"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
            ],
            name="fk_lcds_inventory",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id", "truth_package_market_id", "truth_package_id",
                "target_market", "market_policy_version_id",
            ],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_lcds_market",
        ),
        CheckConstraint("decision_count >= 0", name="ck_lcds_decision_count"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    origin_batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_market: Mapped[str] = mapped_column(String(10), nullable=False)
    market_policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_count: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizationClaimDecisionRecord(Base):
    __tablename__ = "localization_claim_decisions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lcd_workspace_id"),
        UniqueConstraint("workspace_id", "decision_set_id", "claim_index",
                         name="uq_lcd_set_claim_index"),
        ForeignKeyConstraint(
            [
                "workspace_id", "decision_set_id", "inventory_id",
                "truth_package_id", "truth_package_market_id", "target_market",
                "market_policy_version_id",
            ],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_lcd_decision_set",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id", "replacement_fact_id", "truth_package_id",
                "truth_package_market_id", "replacement_fact_scope_key",
            ],
            [
                "truth_facts.workspace_id",
                "truth_facts.id",
                "truth_facts.truth_package_id",
                "truth_facts.truth_package_market_id",
                "truth_facts.scope_key",
            ],
            name="fk_lcd_replacement_fact",
        ),
        CheckConstraint(
            "action IN ('replace', 'remove', 'preserve')",
            name="ck_lcd_action",
        ),
        CheckConstraint(
            "role IN ('owner', 'member')",
            name="ck_lcd_role",
        ),
        CheckConstraint(
            "(action = 'replace' AND replacement_fact_id IS NOT NULL "
            "AND replacement_fact_scope_key = claim_scope_key "
            "AND removal_strategy_json IS NULL AND removal_strategy_hash IS NULL "
            "AND preserve_policy_evidence_hash IS NULL) OR "
            "(action != 'replace' AND replacement_fact_id IS NULL "
            "AND replacement_fact_scope_key IS NULL)",
            name="ck_lcd_action_replace",
        ),
        CheckConstraint(
            "(action = 'remove' AND removal_strategy_json IS NOT NULL "
            "AND removal_strategy_hash IS NOT NULL "
            "AND preserve_policy_evidence_hash IS NULL) OR "
            "(action != 'remove' AND removal_strategy_json IS NULL "
            "AND removal_strategy_hash IS NULL)",
            name="ck_lcd_action_remove",
        ),
        CheckConstraint(
            "(action = 'preserve' AND preserve_policy_evidence_hash IS NOT NULL) OR "
            "(action != 'preserve' AND preserve_policy_evidence_hash IS NULL)",
            name="ck_lcd_action_preserve",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    decision_set_id: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_index: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_scope_key: Mapped[str] = mapped_column(String(120), nullable=False)
    target_market: Mapped[str] = mapped_column(String(10), nullable=False)
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    replacement_fact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replacement_fact_scope_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True,
    )
    removal_strategy_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    removal_strategy_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preserve_policy_evidence_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    rationale: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# Localization -- Batch tables (Issue #24, spec §1.5)
# ---------------------------------------------------------------------------


class LocalizationBatchRecord(Base):
    __tablename__ = "localization_batches"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lb_workspace_id"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         name="uq_lb_workspace_id_truth"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "offer_version_id",
                         name="uq_lb_workspace_id_truth_offer"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "product_version_id", "offer_version_id",
                         name="uq_lb_workspace_id_truth_product_offer"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "provider_execution_mode",
                         name="uq_lb_workspace_id_truth_provider"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "product_version_id", "offer_version_id",
                         "provider_execution_mode",
                         name="uq_lb_workspace_id_truth_product_offer_provider"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "offer_version_id", "evidence_tier",
                         name="uq_lb_workspace_id_truth_offer_tier"),
        UniqueConstraint("workspace_id", "id", "truth_package_id",
                         "product_version_id", "offer_version_id",
                         "truth_data_mode", "provider_execution_mode",
                         "evidence_tier",
                         name="uq_lb_workspace_id_full_provenance"),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_lb_truth_package",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lb_product_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lb_offer_version",
        ),
        CheckConstraint(
            "truth_data_mode IN ('real', 'mock')",
            name="ck_lb_truth_data_mode",
        ),
        CheckConstraint(
            "provider_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lb_provider_execution_mode",
        ),
        CheckConstraint(
            "evidence_tier IS NULL OR "
            "evidence_tier IN ('production', 'sandbox', 'mock')",
            name="ck_lb_evidence_tier",
        ),
        CheckConstraint(
            "phase IN ('draft', 'preparing', 'awaiting_copy_approval', 'ready', "
            "'running', 'closed', 'cancelled')",
            name="ck_lb_phase",
        ),
        CheckConstraint(
            "preparation_stage IS NULL OR preparation_stage IN "
            "('truth', 'analysis', 'claim_review', 'copy', 'complete')",
            name="ck_lb_preparation_stage",
        ),
        CheckConstraint(
            "outcome IN ('none', 'succeeded', 'partially_succeeded', 'failed')",
            name="ck_lb_outcome",
        ),
        CheckConstraint(
            "truth_freshness IS NULL OR "
            "truth_freshness IN ('current', 'superseded', 'expired')",
            name="ck_lb_truth_freshness",
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 3",
            name="ck_lb_max_attempts",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_market_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    truth_package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    truth_data_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    preparation_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    truth_freshness: Mapped[str | None] = mapped_column(String(20), nullable=True)
    frozen_recipe_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    frozen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizationBatchSourceRecord(Base):
    __tablename__ = "localization_batch_sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lbs_workspace_id"),
        UniqueConstraint("workspace_id", "id", "batch_id", "asset_version_id",
                         name="uq_lbs_workspace_id_batch_asset"),
        UniqueConstraint("workspace_id", "id", "batch_id", "asset_version_id",
                         "decision_id",
                         name="uq_lbs_workspace_id_batch_asset_decision"),
        UniqueConstraint("workspace_id", "id", "batch_id", "asset_version_id",
                         "decision_id", "source_generation_mode",
                         "experiment_evidence_mode",
                         name="uq_lbs_workspace_id_full_lineage"),
        UniqueConstraint("workspace_id", "batch_id", "asset_version_id",
                         name="uq_lbs_batch_asset_unique"),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["localization_batches.workspace_id", "localization_batches.id"],
            name="fk_lbs_batch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_lbs_asset_version",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id", "decision_id", "experiment_id",
                "experiment_version_id", "experiment_version_candidate_id",
            ],
            [
                "experiment_decisions.workspace_id",
                "experiment_decisions.id",
                "experiment_decisions.experiment_id",
                "experiment_decisions.experiment_version_id",
                "experiment_decisions.experiment_version_candidate_id",
            ],
            name="fk_lbs_decision",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id", "experiment_version_id",
                "experiment_version_candidate_id", "asset_version_id",
            ],
            [
                "experiment_version_candidates.workspace_id",
                "experiment_version_candidates.experiment_version_id",
                "experiment_version_candidates.id",
                "experiment_version_candidates.asset_version_id",
            ],
            name="fk_lbs_candidate",
        ),
        CheckConstraint(
            "source_generation_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lbs_source_generation_mode",
        ),
        CheckConstraint(
            "experiment_evidence_mode IN ('production', 'sandbox', 'mock')",
            name="ck_lbs_experiment_evidence_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_version_candidate_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    source_generation_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    experiment_evidence_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    source_market_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizationBatchCommandRecord(Base):
    """Append-only command-to-Operation linkage (spec §1.5, §2A).

    ``item_id`` carries a composite FK to
    ``localization_items(workspace_id, id, batch_id)`` -- deferred by Task 5
    (spec §1.5: "added after ``localization_items`` exists in the same
    migration") and now added here in Task 6 alongside
    ``LocalizationItemRecord`` itself (spec §1.7, §1.11 migration DDL order
    step 6). SQLAlchemy resolves the string table/column references lazily
    at mapper configuration time, so this class may declare the FK even
    though ``LocalizationItemRecord`` is defined later in this module.
    """

    __tablename__ = "localization_batch_commands"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lbc_workspace_id"),
        UniqueConstraint("workspace_id", "id", "batch_id",
                         name="uq_lbc_workspace_id_batch"),
        UniqueConstraint("workspace_id", "id", "batch_id", "operation_id",
                         name="uq_lbc_workspace_id_batch_operation"),
        UniqueConstraint("workspace_id", "semantic_key_hash",
                         name="uq_lbc_semantic_key"),
        Index(
            "uq_lbc_operation_id",
            "workspace_id", "operation_id",
            unique=True,
            sqlite_where=text("operation_id IS NOT NULL"),
            postgresql_where=text("operation_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["localization_batches.workspace_id", "localization_batches.id"],
            name="fk_lbc_batch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_lbc_operation",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
            ],
            name="fk_lbc_item",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    command_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    semantic_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizationBatchMarketTargetRecord(Base):
    __tablename__ = "localization_batch_market_targets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lbmt_workspace_id"),
        UniqueConstraint("workspace_id", "batch_id", "market",
                         name="uq_lbmt_batch_market"),
        UniqueConstraint("workspace_id", "id", "batch_id", "market",
                         "market_policy_version_id",
                         name="uq_lbmt_workspace_id_batch_market_mpv"),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["localization_batches.workspace_id", "localization_batches.id"],
            name="fk_lbmt_batch",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("market_policy_versions.id"), nullable=False,
    )
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# Localization -- Copy and translation tables (Issue #24, spec §1.6)
# ---------------------------------------------------------------------------


class LocalizedCopyVersionRecord(Base):
    __tablename__ = "localized_copy_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lcv_workspace_id"),
        UniqueConstraint(
            "workspace_id", "id", "source_asset_version_id", "inventory_id",
            "truth_package_id", "truth_package_market_id", "claim_decision_set_id",
            "target_market", "market_policy_version_id",
            name="uq_lcv_workspace_id_parent",
        ),
        UniqueConstraint(
            "workspace_id", "id", "source_asset_version_id", "inventory_id",
            "truth_package_id", "truth_package_market_id", "claim_decision_set_id",
            "target_market", "market_policy_version_id", "translation_execution_mode",
            name="uq_lcv_workspace_id_translated",
        ),
        UniqueConstraint("workspace_id", "copy_series_hash", "version_number",
                         name="uq_lcv_series_version"),
        ForeignKeyConstraint(
            [
                "workspace_id", "truth_package_market_id", "truth_package_id",
                "target_market", "market_policy_version_id",
            ],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_lcv_market",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id", "claim_decision_set_id", "source_asset_version_id",
                "inventory_id", "truth_package_id", "truth_package_market_id",
                "target_market", "market_policy_version_id",
            ],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.source_asset_version_id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_lcv_decision_set",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_lcv_source_asset_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "inventory_id", "source_asset_version_id"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
            ],
            name="fk_lcv_inventory",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_lcv_truth_package",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "brand_localization_policy_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lcv_brand_policy_version",
        ),
        CheckConstraint(
            "copy_origin IN ('translated', 'source_locale_projection')",
            name="ck_lcv_copy_origin",
        ),
        CheckConstraint(
            "translation_execution_mode IS NULL OR "
            "translation_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lcv_translation_execution_mode",
        ),
        CheckConstraint(
            "state IN ('draft', 'needs_review', 'approved', 'superseded')",
            name="ck_lcv_state",
        ),
        CheckConstraint(
            "(copy_origin = 'translated' AND translation_execution_mode IS NOT NULL "
            "AND translation_provider IS NOT NULL "
            "AND translation_model IS NOT NULL "
            "AND translation_adapter_version IS NOT NULL) OR "
            "(copy_origin = 'source_locale_projection' AND "
            "translation_execution_mode IS NULL AND translation_provider IS NULL "
            "AND translation_model IS NULL AND translation_adapter_version IS NULL)",
            name="ck_lcv_translation_required",
        ),
        CheckConstraint(
            "(translation_execution_mode = 'mock' AND mock_scenario IS NOT NULL "
            "AND mock_version IS NOT NULL) OR "
            "(translation_execution_mode IN ('byok', 'sandbox') AND "
            "mock_scenario IS NULL AND mock_version IS NULL) OR "
            "(translation_execution_mode IS NULL AND mock_scenario IS NULL "
            "AND mock_version IS NULL)",
            name="ck_lcv_translation_mock",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    copy_series_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_decision_set_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_market: Mapped[str] = mapped_column(String(10), nullable=False)
    market_policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    copy_origin: Mapped[str] = mapped_column(String(32), nullable=False)
    translation_execution_mode: Mapped[str | None] = mapped_column(
        String(20), nullable=True,
    )
    translation_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    translation_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    translation_adapter_version: Mapped[str | None] = mapped_column(
        String(40), nullable=True,
    )
    mock_scenario: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mock_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    brand_localization_policy_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    copy_policy_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    copy_policy_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizedCopyApprovalRecord(Base):
    __tablename__ = "localized_copy_approvals"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lca_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "copy_version_id"],
            ["localized_copy_versions.workspace_id", "localized_copy_versions.id"],
            name="fk_lca_copy_version",
        ),
        CheckConstraint(
            "role IN ('owner', 'member')",
            name="ck_lca_role",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    copy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# Localization -- Item and attempt tables (Issue #24, spec §1.7)
# ---------------------------------------------------------------------------


class LocalizationItemRecord(Base):
    """One row per source x target market execution unit (spec §1.7).

    Mutable via CAS (``revision``). The composite FK to
    ``localized_copy_versions`` only bites once ``copy_version_id`` is set
    (a NULL FK column is never checked by the database).
    """

    __tablename__ = "localization_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_li_workspace_id"),
        UniqueConstraint("workspace_id", "id", "batch_id",
                         name="uq_li_workspace_id_batch"),
        UniqueConstraint("workspace_id", "id", "batch_id", "truth_package_id",
                         name="uq_li_workspace_id_batch_truth"),
        UniqueConstraint(
            "workspace_id", "id", "batch_id", "source_asset_version_id",
            "target_market", "market_policy_version_id", "truth_package_id",
            name="uq_li_workspace_id_binding_context",
        ),
        UniqueConstraint(
            "workspace_id", "id", "batch_id", "batch_source_id",
            "source_asset_version_id", "decision_id", "target_market",
            "market_policy_version_id", "truth_package_id",
            "truth_package_market_id", "inventory_id",
            "claim_decision_set_id", "copy_version_id",
            name="uq_li_lineage_target",
        ),
        UniqueConstraint("workspace_id", "batch_id", "batch_source_id",
                         "target_market",
                         name="uq_li_batch_source_market"),
        ForeignKeyConstraint(
            ["workspace_id", "batch_source_id", "batch_id",
             "source_asset_version_id", "decision_id"],
            [
                "localization_batch_sources.workspace_id",
                "localization_batch_sources.id",
                "localization_batch_sources.batch_id",
                "localization_batch_sources.asset_version_id",
                "localization_batch_sources.decision_id",
            ],
            name="fk_li_batch_source",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_market_target_id", "batch_id",
             "target_market", "market_policy_version_id"],
            [
                "localization_batch_market_targets.workspace_id",
                "localization_batch_market_targets.id",
                "localization_batch_market_targets.batch_id",
                "localization_batch_market_targets.market",
                "localization_batch_market_targets.market_policy_version_id",
            ],
            name="fk_li_batch_market_target",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
            ],
            name="fk_li_batch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id",
             "target_market", "market_policy_version_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_li_truth_market",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "claim_decision_set_id",
             "source_asset_version_id", "inventory_id", "truth_package_id",
             "truth_package_market_id", "target_market",
             "market_policy_version_id"],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.source_asset_version_id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_li_claim_decision_set",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "copy_version_id", "source_asset_version_id",
             "inventory_id", "truth_package_id", "truth_package_market_id",
             "claim_decision_set_id", "target_market",
             "market_policy_version_id"],
            [
                "localized_copy_versions.workspace_id",
                "localized_copy_versions.id",
                "localized_copy_versions.source_asset_version_id",
                "localized_copy_versions.inventory_id",
                "localized_copy_versions.truth_package_id",
                "localized_copy_versions.truth_package_market_id",
                "localized_copy_versions.claim_decision_set_id",
                "localized_copy_versions.target_market",
                "localized_copy_versions.market_policy_version_id",
            ],
            name="fk_li_copy_version",
        ),
        CheckConstraint(
            "state IN ('queued', 'running', 'retry_wait', 'paused', "
            "'succeeded', 'failed', 'skipped', 'cancelled', 'uncertain')",
            name="ck_li_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_market_target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_market: Mapped[str] = mapped_column(String(10), nullable=False)
    market_policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("market_policy_versions.id"), nullable=False,
    )
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_decision_set_id: Mapped[str] = mapped_column(String(64), nullable=False)
    copy_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    reason_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recoverable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(60), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizationAttemptRecord(Base):
    """Immutable header per attempt (spec §1.7). At most 3 per Item."""

    __tablename__ = "localization_attempts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_la_workspace_id"),
        UniqueConstraint("workspace_id", "id", "item_id", "batch_id",
                         name="uq_la_workspace_id_item_batch"),
        UniqueConstraint("workspace_id", "item_id", "attempt_number",
                         name="uq_la_item_attempt_number"),
        ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
            ],
            name="fk_la_item",
        ),
        CheckConstraint(
            "attempt_number BETWEEN 1 AND 3",
            name="ck_la_attempt_number",
        ),
        CheckConstraint(
            "state IN ('authorized', 'dispatching', 'completed', 'failed', "
            "'uncertain')",
            name="ck_la_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_versions_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizationAttemptOutputRecord(Base):
    """Immutable output record; no ``qc_passed`` field per spec §2A.

    At most one output per attempt (``UNIQUE(workspace_id, attempt_id)``).
    """

    __tablename__ = "localization_attempt_outputs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lao_workspace_id"),
        UniqueConstraint(
            "workspace_id", "id", "attempt_id", "item_id", "batch_id",
            name="uq_lao_workspace_id_attempt_item_batch",
        ),
        UniqueConstraint("workspace_id", "attempt_id",
                         name="uq_lao_workspace_id_attempt"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempts.workspace_id",
                "localization_attempts.id",
                "localization_attempts.item_id",
                "localization_attempts.batch_id",
            ],
            name="fk_lao_attempt",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    output_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizationOutputPublicationRecord(Base):
    """Single publication event for an attempt output (spec §1.7, §2A).

    Replaces the earlier ``localization_attempt_output_adoptions`` idea:
    one unified record guarantees single-use at the database level for both
    normal finalization and explicit adoption.
    """

    __tablename__ = "localization_output_publications"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lop_workspace_id"),
        UniqueConstraint("workspace_id", "output_id",
                         name="uq_lop_workspace_id_output"),
        UniqueConstraint("workspace_id", "asset_version_id",
                         name="uq_lop_workspace_id_asset_version"),
        UniqueConstraint(
            "workspace_id", "id", "asset_version_id", "item_id", "batch_id",
            name="uq_lop_binding_target",
        ),
        UniqueConstraint(
            "workspace_id", "asset_version_id", "output_id", "attempt_id",
            "item_id", "batch_id",
            name="uq_lop_lineage_target",
        ),
        UniqueConstraint(
            "workspace_id", "id", "asset_version_id", "output_id",
            "attempt_id", "item_id", "batch_id",
            name="uq_lop_identity_lineage_target",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "output_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempt_outputs.workspace_id",
                "localization_attempt_outputs.id",
                "localization_attempt_outputs.attempt_id",
                "localization_attempt_outputs.item_id",
                "localization_attempt_outputs.batch_id",
            ],
            name="fk_lop_output",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_lop_asset_version",
        ),
        CheckConstraint(
            "publication_kind IN ('normal', 'adopted')",
            name="ck_lop_publication_kind",
        ),
        CheckConstraint(
            "role IN ('owner', 'member')",
            name="ck_lop_role",
        ),
        CheckConstraint(
            "(publication_kind = 'adopted' AND recheck_hash IS NOT NULL) OR "
            "(publication_kind = 'normal' AND recheck_hash IS NULL)",
            name="ck_lop_recheck_hash",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    output_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    recheck_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# Localization -- QC tables (Issue #24, spec §1.8)
# ---------------------------------------------------------------------------


class LocalizationQcRecordRecord(Base):
    """Append-only per-check record for each attempt (spec §1.8)."""

    __tablename__ = "localization_qc_records"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lqr_workspace_id"),
        UniqueConstraint("workspace_id", "attempt_id", "check_slot_key",
                         name="uq_lqr_attempt_slot"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempts.workspace_id",
                "localization_attempts.id",
                "localization_attempts.item_id",
                "localization_attempts.batch_id",
            ],
            name="fk_lqr_attempt",
        ),
        CheckConstraint(
            "severity IN ('blocking', 'warning')",
            name="ck_lqr_severity",
        ),
        CheckConstraint(
            "result IN ('pass', 'fail')",
            name="ck_lqr_result",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    check_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    check_slot_key: Mapped[str] = mapped_column(String(120), nullable=False)
    expected_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence_location_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[str] = mapped_column(String(10), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# Localization -- Provider call join tables (Issue #24, spec §1.9)
# ---------------------------------------------------------------------------


class LocalizationItemProviderCallRecord(Base):
    """Links item-phase localization attempts to ``provider_call_attempts``
    (edit, OCR, visual QC) -- spec §1.9.

    ``provider_call_attempts`` is declared in ``app.entitlements.cost_models``
    on the same shared ``Base``; the composite FK below resolves through
    SQLAlchemy's lazy string-based table lookup (see the
    ``app.entitlements.cost_models`` import at the bottom of this module).
    """

    __tablename__ = "localization_item_provider_calls"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lipc_workspace_id"),
        UniqueConstraint("workspace_id", "provider_call_attempt_id",
                         name="uq_lipc_provider_call_attempt"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempts.workspace_id",
                "localization_attempts.id",
                "localization_attempts.item_id",
                "localization_attempts.batch_id",
            ],
            name="fk_lipc_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id", "truth_package_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
                "localization_items.truth_package_id",
            ],
            name="fk_lipc_item",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id",
             "provider_execution_mode"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
                "localization_batches.provider_execution_mode",
            ],
            name="fk_lipc_batch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_command_id", "batch_id", "operation_id"],
            [
                "localization_batch_commands.workspace_id",
                "localization_batch_commands.id",
                "localization_batch_commands.batch_id",
                "localization_batch_commands.operation_id",
            ],
            name="fk_lipc_batch_command",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "provider_call_attempt_id", "operation_id",
             "truth_package_id", "provider_step_kind",
             "provider_execution_mode"],
            [
                "provider_call_attempts.workspace_id",
                "provider_call_attempts.id",
                "provider_call_attempts.operation_id",
                "provider_call_attempts.truth_package_id",
                "provider_call_attempts.step_kind",
                "provider_call_attempts.execution_mode",
            ],
            name="fk_lipc_provider_call_attempt",
        ),
        CheckConstraint(
            "provider_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lipc_exec_mode",
        ),
        CheckConstraint(
            "provider_step_kind IN ('localization_edit', 'localization_ocr', "
            "'localization_visual_qc')",
            name="ck_lipc_provider_step_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_command_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_step_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_call_attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)


class LocalizationPreparationProviderCallRecord(Base):
    """Persistent preparation work + phase-1 ProviderAttempt link (spec §1.9).

    The row exists before dispatch and is the domain subject used by
    ``ExternalCallEvidenceRecorder``; the immutable Inventory or CopyVersion
    it eventually produces does not need to exist yet. Result IDs are
    write-once, filled in the same transaction as ProviderAttempt success
    evidence.
    """

    __tablename__ = "localization_preparation_provider_calls"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lppc_workspace_id"),
        UniqueConstraint("workspace_id", "subject_semantic_key_hash",
                         name="uq_lppc_subject_semantic_key"),
        UniqueConstraint("workspace_id", "provider_call_attempt_id",
                         name="uq_lppc_provider_call_attempt"),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id",
             "provider_execution_mode"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
                "localization_batches.provider_execution_mode",
            ],
            name="fk_lppc_batch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_source_id", "batch_id",
             "source_asset_version_id"],
            [
                "localization_batch_sources.workspace_id",
                "localization_batch_sources.id",
                "localization_batch_sources.batch_id",
                "localization_batch_sources.asset_version_id",
            ],
            name="fk_lppc_batch_source",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_command_id", "batch_id", "operation_id"],
            [
                "localization_batch_commands.workspace_id",
                "localization_batch_commands.id",
                "localization_batch_commands.batch_id",
                "localization_batch_commands.operation_id",
            ],
            name="fk_lppc_batch_command",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "input_inventory_id", "source_asset_version_id"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
            ],
            name="fk_lppc_input_inventory",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "result_inventory_id", "source_asset_version_id",
             "provider_execution_mode"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
                "source_claim_inventories.analysis_execution_mode",
            ],
            name="fk_lppc_result_inventory",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id",
             "target_market", "market_policy_version_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_lppc_truth_market",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "claim_decision_set_id",
             "source_asset_version_id", "input_inventory_id",
             "truth_package_id", "truth_package_market_id", "target_market",
             "market_policy_version_id"],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.source_asset_version_id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_lppc_claim_decision_set",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "result_copy_version_id",
             "source_asset_version_id", "input_inventory_id",
             "truth_package_id", "truth_package_market_id",
             "claim_decision_set_id", "target_market",
             "market_policy_version_id", "provider_execution_mode"],
            [
                "localized_copy_versions.workspace_id",
                "localized_copy_versions.id",
                "localized_copy_versions.source_asset_version_id",
                "localized_copy_versions.inventory_id",
                "localized_copy_versions.truth_package_id",
                "localized_copy_versions.truth_package_market_id",
                "localized_copy_versions.claim_decision_set_id",
                "localized_copy_versions.target_market",
                "localized_copy_versions.market_policy_version_id",
                "localized_copy_versions.translation_execution_mode",
            ],
            name="fk_lppc_result_copy_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "provider_call_attempt_id", "operation_id",
             "truth_package_id", "provider_step_kind",
             "provider_execution_mode"],
            [
                "provider_call_attempts.workspace_id",
                "provider_call_attempts.id",
                "provider_call_attempts.operation_id",
                "provider_call_attempts.truth_package_id",
                "provider_call_attempts.step_kind",
                "provider_call_attempts.execution_mode",
            ],
            name="fk_lppc_provider_call_attempt",
        ),
        CheckConstraint(
            "provider_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lppc_exec_mode",
        ),
        CheckConstraint(
            "work_kind IN ('analysis', 'translation')",
            name="ck_lppc_work_kind",
        ),
        CheckConstraint(
            "provider_step_kind IN ('source_analysis', 'translation')",
            name="ck_lppc_step_kind",
        ),
        CheckConstraint(
            "state IN ('prepared', 'succeeded', 'failed', 'uncertain')",
            name="ck_lppc_state",
        ),
        CheckConstraint(
            "(work_kind = 'analysis' AND provider_step_kind = 'source_analysis' "
            "AND input_inventory_id IS NULL AND truth_package_market_id IS NULL "
            "AND target_market IS NULL AND market_policy_version_id IS NULL "
            "AND claim_decision_set_id IS NULL) OR "
            "(work_kind = 'translation' AND provider_step_kind = 'translation' "
            "AND input_inventory_id IS NOT NULL "
            "AND truth_package_market_id IS NOT NULL "
            "AND target_market IS NOT NULL "
            "AND market_policy_version_id IS NOT NULL "
            "AND claim_decision_set_id IS NOT NULL)",
            name="ck_lppc_kind_check",
        ),
        CheckConstraint(
            "(state = 'succeeded' AND completed_at IS NOT NULL AND "
            "((work_kind = 'analysis' AND result_inventory_id IS NOT NULL "
            "AND result_copy_version_id IS NULL) OR "
            "(work_kind = 'translation' AND result_copy_version_id IS NOT NULL "
            "AND result_inventory_id IS NULL))) OR "
            "(state != 'succeeded' AND result_inventory_id IS NULL AND "
            "result_copy_version_id IS NULL)",
            name="ck_lppc_done_check",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_command_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    work_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_step_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    input_inventory_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    truth_package_market_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    target_market: Mapped[str | None] = mapped_column(String(10), nullable=True)
    market_policy_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    claim_decision_set_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    subject_semantic_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_call_attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result_inventory_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_copy_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class LocalizationSemanticClaimRecord(Base):
    """Durable workspace-level canonical claim (spec §1.9).

    Prevents concurrent or later Batches from duplicating paid work on the
    same semantic intent. Created atomically at Item admission and never
    released or deleted.
    """

    __tablename__ = "localization_semantic_claims"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lsc_workspace_id"),
        UniqueConstraint("workspace_id", "item_fingerprint_hash",
                         name="uq_lsc_item_fingerprint"),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["localization_batches.workspace_id", "localization_batches.id"],
            name="fk_lsc_batch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
            ],
            name="fk_lsc_item",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "current_operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_lsc_current_operation",
        ),
        CheckConstraint(
            "claim_state IN ('active', 'succeeded', 'failed', 'cancelled')",
            name="ck_lsc_claim_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    item_fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    current_operation_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    claim_state: Mapped[str] = mapped_column(String(20), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# ---------------------------------------------------------------------------
# Localization -- Binding and lineage tables (Issue #24, spec §1.10)
# ---------------------------------------------------------------------------


class MarketAssetBindingRecord(Base):
    """Persistent relationship between a source AssetVersion and its
    target-market representation (spec §1.10). Mutable via CAS.
    """

    __tablename__ = "market_asset_bindings"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_mab_workspace_id"),
        Index(
            "uq_mab_active_binding",
            "workspace_id", "source_asset_version_id", "target_market",
            "offer_version_id",
            unique=True,
            sqlite_where=text("active = true"),
            postgresql_where=text("active = true"),
        ),
        Index(
            "uq_mab_publication",
            "workspace_id", "publication_id",
            unique=True,
            sqlite_where=text("publication_id IS NOT NULL"),
            postgresql_where=text("publication_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id", "source_asset_version_id",
             "target_market", "market_policy_version_id", "truth_package_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
                "localization_items.source_asset_version_id",
                "localization_items.target_market",
                "localization_items.market_policy_version_id",
                "localization_items.truth_package_id",
            ],
            name="fk_mab_item",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "publication_id", "bound_asset_version_id",
             "item_id", "batch_id"],
            [
                "localization_output_publications.workspace_id",
                "localization_output_publications.id",
                "localization_output_publications.asset_version_id",
                "localization_output_publications.item_id",
                "localization_output_publications.batch_id",
            ],
            name="fk_mab_publication",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_mab_source_asset_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "bound_asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_mab_bound_asset_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id",
             "offer_version_id", "evidence_tier"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
                "localization_batches.offer_version_id",
                "localization_batches.evidence_tier",
            ],
            name="fk_mab_batch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_mab_truth_package",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_mab_offer_version",
        ),
        CheckConstraint(
            "binding_kind IN ('localized', 'source_reuse')",
            name="ck_mab_binding_kind",
        ),
        CheckConstraint(
            "review_state IN ('pending_review', 'approved', 'rejected', "
            "'needs_changes')",
            name="ck_mab_review_state",
        ),
        CheckConstraint(
            "truth_freshness IN ('current', 'stale')",
            name="ck_mab_truth_freshness",
        ),
        CheckConstraint(
            "evidence_tier IN ('production', 'sandbox', 'mock')",
            name="ck_mab_evidence_tier",
        ),
        CheckConstraint(
            "(binding_kind = 'localized' AND publication_id IS NOT NULL) OR "
            "(binding_kind = 'source_reuse' AND publication_id IS NULL AND "
            "bound_asset_version_id = source_asset_version_id)",
            name="ck_mab_binding_kind_consistency",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    source_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_market: Mapped[str] = mapped_column(String(10), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    publication_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_state: Mapped[str] = mapped_column(String(20), nullable=False)
    truth_freshness: Mapped[str] = mapped_column(String(20), nullable=False)
    ready_for_experiment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class MarketAssetBindingEventRecord(Base):
    """Append-only lifecycle events for bindings (spec §1.10)."""

    __tablename__ = "market_asset_binding_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_mabe_workspace_id"),
        UniqueConstraint("workspace_id", "binding_id", "sequence",
                         name="uq_mabe_binding_sequence"),
        Index(
            "uq_mabe_dependency_change",
            "workspace_id", "binding_id", "dependency_change_id",
            unique=True,
            sqlite_where=text("dependency_change_id IS NOT NULL"),
            postgresql_where=text("dependency_change_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "binding_id"],
            ["market_asset_bindings.workspace_id", "market_asset_bindings.id"],
            name="fk_mabe_binding",
        ),
        CheckConstraint(
            "sequence >= 1",
            name="ck_mabe_sequence",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    binding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    dependency_change_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    previous_state_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    new_state_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


class LocalizedAssetLineageRecord(Base):
    """Immutable provenance record for every published localized
    AssetVersion (spec §1.10). Composite FKs chain across the entire
    Batch -> Source -> Item -> Attempt -> Output -> Publication path plus
    Truth, Claim, Copy, Decision, and AssetVersion provenance.
    """

    __tablename__ = "localized_asset_lineages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lal_workspace_id"),
        UniqueConstraint("workspace_id", "asset_version_id",
                         name="uq_lal_workspace_id_asset_version"),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id",
             "product_version_id", "offer_version_id", "truth_data_mode",
             "provider_execution_mode", "evidence_tier"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
                "localization_batches.product_version_id",
                "localization_batches.offer_version_id",
                "localization_batches.truth_data_mode",
                "localization_batches.provider_execution_mode",
                "localization_batches.evidence_tier",
            ],
            name="fk_lal_batch",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "batch_source_id", "batch_id",
             "source_asset_version_id", "decision_id",
             "source_generation_mode", "experiment_evidence_mode"],
            [
                "localization_batch_sources.workspace_id",
                "localization_batch_sources.id",
                "localization_batch_sources.batch_id",
                "localization_batch_sources.asset_version_id",
                "localization_batch_sources.decision_id",
                "localization_batch_sources.source_generation_mode",
                "localization_batch_sources.experiment_evidence_mode",
            ],
            name="fk_lal_batch_source",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id", "batch_source_id",
             "source_asset_version_id", "decision_id", "target_market",
             "market_policy_version_id", "truth_package_id",
             "truth_package_market_id", "inventory_id",
             "claim_decision_set_id", "copy_version_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
                "localization_items.batch_source_id",
                "localization_items.source_asset_version_id",
                "localization_items.decision_id",
                "localization_items.target_market",
                "localization_items.market_policy_version_id",
                "localization_items.truth_package_id",
                "localization_items.truth_package_market_id",
                "localization_items.inventory_id",
                "localization_items.claim_decision_set_id",
                "localization_items.copy_version_id",
            ],
            name="fk_lal_item",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempts.workspace_id",
                "localization_attempts.id",
                "localization_attempts.item_id",
                "localization_attempts.batch_id",
            ],
            name="fk_lal_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "output_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempt_outputs.workspace_id",
                "localization_attempt_outputs.id",
                "localization_attempt_outputs.attempt_id",
                "localization_attempt_outputs.item_id",
                "localization_attempt_outputs.batch_id",
            ],
            name="fk_lal_output",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "publication_id", "asset_version_id", "output_id",
             "attempt_id", "item_id", "batch_id"],
            [
                "localization_output_publications.workspace_id",
                "localization_output_publications.id",
                "localization_output_publications.asset_version_id",
                "localization_output_publications.output_id",
                "localization_output_publications.attempt_id",
                "localization_output_publications.item_id",
                "localization_output_publications.batch_id",
            ],
            name="fk_lal_publication",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id",
             "target_market", "market_policy_version_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_lal_truth_market",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "claim_decision_set_id",
             "source_asset_version_id", "inventory_id", "truth_package_id",
             "truth_package_market_id", "target_market",
             "market_policy_version_id"],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.source_asset_version_id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_lal_claim_decision_set",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "copy_version_id", "source_asset_version_id",
             "inventory_id", "truth_package_id", "truth_package_market_id",
             "claim_decision_set_id", "target_market",
             "market_policy_version_id"],
            [
                "localized_copy_versions.workspace_id",
                "localized_copy_versions.id",
                "localized_copy_versions.source_asset_version_id",
                "localized_copy_versions.inventory_id",
                "localized_copy_versions.truth_package_id",
                "localized_copy_versions.truth_package_market_id",
                "localized_copy_versions.claim_decision_set_id",
                "localized_copy_versions.target_market",
                "localized_copy_versions.market_policy_version_id",
            ],
            name="fk_lal_copy_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_version_id", "truth_package_id",
             "source_asset_version_id"],
            [
                "asset_versions.workspace_id",
                "asset_versions.id",
                "asset_versions.truth_package_id",
                "asset_versions.parent_version_id",
            ],
            name="fk_lal_localized_asset_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_lal_source_asset_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "decision_id"],
            ["experiment_decisions.workspace_id", "experiment_decisions.id"],
            name="fk_lal_decision",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_package_id", "truth_data_mode"],
            ["truth_packages.workspace_id", "truth_packages.id",
             "truth_packages.truth_data_mode"],
            name="fk_lal_truth_package",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "inventory_id", "source_asset_version_id"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
            ],
            name="fk_lal_inventory",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lal_product_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lal_offer_version",
        ),
        CheckConstraint(
            "source_generation_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lal_source_generation_mode",
        ),
        CheckConstraint(
            "experiment_evidence_mode IN ('production', 'sandbox', 'mock')",
            name="ck_lal_experiment_evidence_mode",
        ),
        CheckConstraint(
            "truth_data_mode IN ('real', 'mock')",
            name="ck_lal_truth_data_mode",
        ),
        CheckConstraint(
            "provider_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lal_provider_execution_mode",
        ),
        CheckConstraint(
            "evidence_tier IN ('production', 'sandbox', 'mock')",
            name="ck_lal_evidence_tier",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    output_id: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_decision_set_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_market: Mapped[str] = mapped_column(String(10), nullable=False)
    target_market: Mapped[str] = mapped_column(String(10), nullable=False)
    truth_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_package_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    copy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_generation_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    experiment_evidence_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    truth_data_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )


# `GenerationRunRecord.__table_args__` above carries ForeignKeyConstraints into
# `generation_recipe_snapshots` and `resolution_preview_confirmations`, both
# declared in `app.generation.models` (Issue #21). SQLAlchemy resolves those
# string table references lazily -- the first flush of any mapped class whose
# table has a foreign key computes `Mapper._sorted_tables`, which touches
# every FK's target table -- so any process that persists a row through this
# module (worker, API, scripts) must have `app.generation.models` imported
# before that happens, or resolution raises `NoReferencedTableError`. This
# import is placed at the bottom, after every class in this module is
# defined, to avoid a circular import: `app.generation.models` itself imports
# `new_id`/`utcnow` from this module.
from app.generation import models as _generation_models  # noqa: E402,F401

# `LocalizationItemProviderCallRecord` and
# `LocalizationPreparationProviderCallRecord` above (Issue #24, spec §1.9)
# carry composite ForeignKeyConstraints into `provider_call_attempts`,
# declared in `app.entitlements.cost_models` on this same shared `Base`. For
# the same reason as the `app.generation.models` import above, any process
# that persists a row through this module needs that table registered in
# `Base.metadata` first. `app.entitlements.cost_models` does not import this
# module directly (only transitively via `app.generation.models`, which it
# also imports at its own bottom), so importing it here after
# `app.generation.models` is already fully loaded is circular-import-safe.
from app.entitlements import cost_models as _cost_models  # noqa: E402,F401
from app.meta import models as _meta_models  # noqa: E402,F401
from app.launch import models as _launch_models  # noqa: E402,F401
