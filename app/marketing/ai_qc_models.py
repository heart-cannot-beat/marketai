"""Exact-image, exact-intent AI QC attempts and their paid dispatch fences."""
from datetime import datetime
from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.adapters.persistence.database import Base
from app.adapters.persistence.models import utcnow


class MarketingAIQCRecord(Base):
    __tablename__ = "marketing_ai_qc_calls"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_marketing_ai_qc_workspace_id"),
        UniqueConstraint("workspace_id", "round_id", "asset_version_id", "request_hash", name="uq_marketing_ai_qc_request"),
        ForeignKeyConstraint(["workspace_id", "round_id"], ["marketing_rounds.workspace_id", "marketing_rounds.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "asset_version_id"], ["asset_versions.workspace_id", "asset_versions.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "operation_id"], ["operation_records.workspace_id", "operation_records.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "job_id"], ["jobs.workspace_id", "jobs.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "retry_of_id"], ["marketing_ai_qc_calls.workspace_id", "marketing_ai_qc_calls.id"], ondelete="RESTRICT"),
        CheckConstraint("kind IS NULL OR kind IN ('single', 'paired')", name="ai_qc_kind"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # single | paired; NULL is a call from before the split that checked everything at once.
    kind: Mapped[str | None] = mapped_column(String(16))
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    retry_of_id: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    maximum_cost: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    # Why a returned output was refused (#295): reason, field path, bounded excerpt and hash of the output; never a request.
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
