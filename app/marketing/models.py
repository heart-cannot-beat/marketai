"""Marketing planning aggregates and immutable revision history."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


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
