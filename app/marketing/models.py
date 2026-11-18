"""Marketing planning aggregates and immutable revision history."""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, CheckConstraint, DateTime, Index, ForeignKey, ForeignKeyConstraint, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import utcnow


class MarketingProjectRecord(Base):
    __tablename__ = "marketing_projects"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_marketing_projects_workspace_id"),
        Index("ix_marketing_projects_history", "workspace_id", "created_at", "id"),
        ForeignKeyConstraint(["workspace_id", "product_id"], ["products.workspace_id", "products.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    product_id: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="input")
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    understanding_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # ADR 0018: "plan_first" projects keep the plan-confirmed flow; "ads_first" projects hold
    # ideas (market, ad language and the Ideas document) and never a plan in step 1.
    flow: Mapped[str] = mapped_column(String(20), nullable=False, default="plan_first", server_default="plan_first")
    market: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ad_language: Mapped[str | None] = mapped_column(String(35), nullable=True)
    ideas_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MarketingFirstBatchRecord(Base):
    """One "start" of the ads-first opening (#193): read the product, draft, generate, under one confirmed maximum.

    The steps stay ordinary records -- the product intake, the planning calls, the generation runs -- each
    reserved and settled on its own. This row only remembers which intake it began with, the product and
    ideas it created, the maximum the user confirmed, the maxima reserved so far and why it stopped.
    """
    __tablename__ = "marketing_first_batches"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_marketing_first_batches_workspace_id"),
        UniqueConstraint("workspace_id", "intake_id", name="uq_marketing_first_batch_intake"),
        Index("ix_marketing_first_batches_project", "workspace_id", "project_id"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"],
                             ondelete="RESTRICT", name="fk_marketing_first_batch_project"),
        ForeignKeyConstraint(["workspace_id", "product_id"], ["products.workspace_id", "products.id"],
                             ondelete="RESTRICT", name="fk_marketing_first_batch_product"),
        ForeignKeyConstraint(["workspace_id", "operation_id"], ["operation_records.workspace_id", "operation_records.id"],
                             ondelete="RESTRICT", name="fk_marketing_first_batch_operation"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    intake_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market: Mapped[str] = mapped_column(String(80), nullable=False)
    ad_language: Mapped[str] = mapped_column(String(35), nullable=False)
    strategy_language: Mapped[str] = mapped_column(String(10), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_variant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attention_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The image settings confirmed with the price and every step admitted so far, with its maximum.
    progress_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    confirmed_max_cost: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_total: Mapped[str] = mapped_column(String(32), nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    initiator_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MarketingAdKeepRecord(Base):
    """The user's current "keep" mark on one ad of an ads_first project; changes are audited."""
    __tablename__ = "marketing_ad_keeps"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "asset_version_id", name="uq_marketing_ad_keep"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"],
                             ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kept: Mapped[bool] = mapped_column(Boolean, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MarketingIdeasExportRecord(Base):
    """Append-only evidence of what took ads-first ads from draft to download (#198, ADR 0018).

    ``verification``: the facts one "check before download" confirmed, the ads whose appearance the user
    confirmed, the check findings the user accepted, and the product version it published.
    ``download``: the ads one download handed out and the download intents it created. Never updated: an
    ad's status is derived from these rows, the project's facts and the image's check.
    """
    __tablename__ = "marketing_ideas_export_records"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_marketing_ideas_export_records_workspace_id"),
        Index("ix_marketing_ideas_export_records_project", "workspace_id", "project_id"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"],
                             ondelete="RESTRICT", name="fk_marketing_ideas_export_record_project"),
        CheckConstraint("kind IN ('verification', 'download')", name="record_kind"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # The project revision the decision produced (a verification) or was made at (a download).
    project_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The creative-library product version a verification published, if it published one.
    library_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MarketingIdeasUploadRecord(Base):
    """An image the user uploaded, placed under one ad of ads-first ideas (#200, design doc §5.3).

    The image is an external creative (#168) of the project's product whose uploader declared the right
    to use it. ``intent_json`` is the ad's ``ideas_intent`` when the user placed it: the claim, the way of
    telling, the understanding as it was and the facts the ad cites, so the image is checked, verified and
    downloaded like a generated one. Never updated or deleted; one image is one ad of the ideas."""
    __tablename__ = "marketing_ideas_uploads"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_marketing_ideas_uploads_workspace_id"),
        UniqueConstraint("workspace_id", "project_id", "asset_version_id", name="uq_marketing_ideas_upload_ad"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"],
                             ondelete="RESTRICT", name="fk_marketing_ideas_upload_project"),
        ForeignKeyConstraint(["workspace_id", "asset_version_id"], ["asset_versions.workspace_id", "asset_versions.id"],
                             ondelete="RESTRICT", name="fk_marketing_ideas_upload_asset_version"),
        ForeignKeyConstraint(["workspace_id", "external_creative_id"], ["external_creatives.workspace_id", "external_creatives.id"],
                             ondelete="RESTRICT", name="fk_marketing_ideas_upload_external_creative"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    combination_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_creative_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MarketingRevisionRecord(Base):
    __tablename__ = "marketing_revisions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "revision", name="uq_marketing_revision"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PlanningCallRecord(Base):
    __tablename__ = "marketing_planning_calls"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "operation_id"], ["operation_records.workspace_id", "operation_records.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "job_id"], ["jobs.workspace_id", "jobs.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    input_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    output_json: Mapped[dict | None] = mapped_column(JSON)
    maximum_cost: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MarketingGenerationLinkRecord(Base):
    __tablename__ = "marketing_generation_links"
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "plan_revision", "combination_id", name="uq_marketing_generation_link"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "draft_id"], ["generation_drafts.workspace_id", "generation_drafts.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    combination_id: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(64), nullable=False)


class ProductBrandKitRecord(Base):
    """Append-only brand baseline versions; a product's current kit is its latest version."""
    __tablename__ = "product_brand_kits"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_product_brand_kit_workspace_id"),
        UniqueConstraint("workspace_id", "product_id", "version", name="uq_product_brand_kit_version"),
        ForeignKeyConstraint(["workspace_id", "product_id"], ["products.workspace_id", "products.id"],
                             ondelete="RESTRICT", name="fk_product_brand_kit_product"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
