"""Delivery evidence is append-only and independent of mutable planning drafts."""
from datetime import date, datetime

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.adapters.persistence.database import Base
from app.adapters.persistence.models import utcnow


class MarketingRoundRecord(Base):
    __tablename__ = "marketing_rounds"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_marketing_rounds_workspace_id"),
        UniqueConstraint("workspace_id", "project_id", "plan_revision", name="uq_marketing_round_plan"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["marketing_projects.workspace_id", "marketing_projects.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="reviewing")
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    close_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketingQCRecord(Base):
    __tablename__ = "marketing_qc_reviews"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "round_id"], ["marketing_rounds.workspace_id", "marketing_rounds.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "asset_version_id"], ["asset_versions.workspace_id", "asset_versions.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "quality_review_id"], ["quality_reviews.workspace_id", "quality_reviews.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    findings_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    quality_review_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketingCreativeReuseRecord(Base):
    """An existing execution mapped to a new intent, without rewriting its origin."""
    __tablename__ = "marketing_creative_reuses"
    __table_args__ = (
        UniqueConstraint("workspace_id", "round_id", "asset_version_id", name="uq_marketing_creative_reuse"),
        ForeignKeyConstraint(["workspace_id", "round_id"], ["marketing_rounds.workspace_id", "marketing_rounds.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "asset_version_id"], ["asset_versions.workspace_id", "asset_versions.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    combination_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketingFeedbackDraftRecord(Base):
    """Immutable QC feedback and exact source lineage for a correction draft."""
    __tablename__ = "marketing_feedback_drafts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "draft_id", name="uq_marketing_feedback_draft"),
        UniqueConstraint("workspace_id", "round_id", "feedback_hash", name="uq_marketing_feedback_intent"),
        *[ForeignKeyConstraint(["workspace_id", col], [table + ".workspace_id", table + ".id"], ondelete="RESTRICT", name="fk_image_edit_" + col)
          for col, table in (("round_id", "marketing_rounds"), ("source_asset_version_id", "asset_versions"), ("draft_id", "generation_drafts"))],
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketingImageEditRecord(Base):
    """Frozen local image operation; source mode is provenance, never a paid call."""
    __tablename__ = "marketing_image_edits"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_marketing_image_edit_workspace_id"),
        UniqueConstraint("workspace_id", "round_id", "input_hash", name="uq_marketing_image_edit_input"),
        CheckConstraint("state IN ('queued', 'succeeded', 'unchanged', 'failed')", name="image_edit_state"),
        *[ForeignKeyConstraint(["workspace_id", col], [table + ".workspace_id", table + ".id"], ondelete="RESTRICT", name="fk_image_edit_" + col)
          for col, table in (("round_id", "marketing_rounds"), ("source_asset_version_id", "asset_versions"),
                             ("result_asset_version_id", "asset_versions"), ("operation_id", "operation_records"))],
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result_asset_version_id: Mapped[str | None] = mapped_column(String(64))
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketingHandoffRecord(Base):
    __tablename__ = "marketing_handoffs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_marketing_handoffs_workspace_id"),
        UniqueConstraint("workspace_id", "round_id", name="uq_marketing_handoff_round"),
        ForeignKeyConstraint(["workspace_id", "round_id"], ["marketing_rounds.workspace_id", "marketing_rounds.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "operation_id"], ["operation_records.workspace_id", "operation_records.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    blob_json: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketingAdBindingRecord(Base):
    __tablename__ = "marketing_ad_bindings"
    __table_args__ = (
        UniqueConstraint("workspace_id", "round_id", "ad_account_id", "ad_id", name="uq_marketing_ad_round"),
        ForeignKeyConstraint(["workspace_id", "round_id"], ["marketing_rounds.workspace_id", "marketing_rounds.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "handoff_id"], ["marketing_handoffs.workspace_id", "marketing_handoffs.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(64), nullable=False)
    handoff_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ad_id: Mapped[str] = mapped_column(String(64), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    conditions_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketingMetricBatchRecord(Base):
    __tablename__ = "marketing_metric_batches"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_marketing_metric_batches_workspace_id"),
        UniqueConstraint("workspace_id", "round_id", "request_hash", name="uq_marketing_metric_request"),
        ForeignKeyConstraint(["workspace_id", "round_id"], ["marketing_rounds.workspace_id", "marketing_rounds.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "replaces_id"], ["marketing_metric_batches.workspace_id", "marketing_metric_batches.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(64), nullable=False)
    replaces_id: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_csv: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    rows_json: Mapped[list] = mapped_column(JSON, nullable=False)
    issues_json: Mapped[list] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MarketingEvaluationRecord(Base):
    __tablename__ = "marketing_evaluations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "round_id", "input_hash", name="uq_marketing_evaluation_input"),
        ForeignKeyConstraint(["workspace_id", "round_id"], ["marketing_rounds.workspace_id", "marketing_rounds.id"], ondelete="RESTRICT"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
