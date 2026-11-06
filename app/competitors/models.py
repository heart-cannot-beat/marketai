"""Competitor list, resolves and page confirmations (spec §3, §4, §11; Issue #269).

* ``competitors`` -- current state: a product's comparison target (or its
  own page, ``role='self'``), how it was added and whether it is archived.
  Its confirmed page is not stored here; it is the latest confirmation.
* ``competitor_resolutions`` -- current state of one paid resolve Job
  (``competitor.resolve``): the frozen quote, the candidates, their §4.2
  scores and domain probes, and the settled cost. Reusable for confirmation
  for 72 hours after it finishes (§4.4).
* ``competitor_identity_confirmations`` -- append-only: each confirmation
  of a page is a new revision; scans read the latest one. PostgreSQL
  refuses UPDATE and DELETE with the shared append-only trigger.

Scans (spec §5, §11; Issue #270):

* ``competitor_scans`` -- current state of one paid scan: status and
  stage, frozen params, the first key version (later ones are in
  ``evidence_json.resumes``), step evidence and both costs. A resume keeps
  the scan ID and points ``job_id`` / ``operation_id`` at its own Job.
* ``competitor_ads`` -- append-only observations: one row per
  ``ad_archive_id`` per scan, with no competitor column.
* ``competitor_ad_origins`` -- append-only: every path an observation was
  found by (``page:<competitor_id>`` / ``keyword:<keyword>``); which
  competitors an observation belongs to is projected from its ``page:``
  rows, so a later page origin is appended, never an UPDATE.

Judgments (spec §7.1, §11; Issue #271):

* ``competitor_ad_assessments`` -- append-only revisions of an
  observation's judgment of one ``kind`` (``relevance``, Issue #271;
  ``tags``, Issue #272). The newest revision is in effect; a flip or a tag
  confirmation is a new revision, so changing one's mind is history, never
  an UPDATE.

Clusters and snapshots (spec §7.1, §11; Issue #272):

* ``competitor_scan_assessments`` -- append-only revisions of the scan's
  buying-claim clusters (``kind='clusters'``): each cluster step and each
  paid recluster appends one; the newest is in effect.
* ``competitor_scan_snapshots`` -- append-only, pure-function freezes of a
  scan's observations and the revisions in effect, unique by content hash
  per scan (an unchanged refreeze returns the existing row). A plan cites
  one by ID (``competitor_snapshot_id``, C-24).

Discovery (spec §8, §11; Issue #276):

* ``competitor_discovery_decisions`` -- append-only: a member's "accept" or
  "reject" of an advertiser a scan's keywords found and the discover step
  recommended, per product. The latest decision per advertiser page is in
  effect; an accept also created a competitor confirmed by that page.

All of them are Workspace-scoped (composite foreign keys + RLS).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import utcnow

ROLES = ("competitor", "self")
STATUSES = ("active", "archived")
INPUT_KINDS = ("name", "facebook_page", "ad_library")
RESOLUTION_STATUSES = ("queued", "running", "completed", "failed", "uncertain")
EXECUTION_MODES = ("byok", "mock")
CONFIRMATION_SOURCES = ("link", "auto", "user")
SCAN_STATUSES = ("queued", "running", "completed", "partial", "failed", "uncertain")
#: A scan in one of these has stopped (a ``partial`` one may still be resumed).
SCAN_FINISHED_STATUSES = ("completed", "partial", "failed", "uncertain")
SCAN_STAGES = ("fetch", "media", "filter", "tag", "cluster", "freeze")
ASSESSMENT_KINDS = ("relevance", "tags")
ASSESSMENT_SOURCES = ("ai", "rule", "user")
RELEVANCE_VALUES = ("relevant", "adjacent", "unrelated")
SCAN_ASSESSMENT_KINDS = ("clusters",)
SCAN_ASSESSMENT_SOURCES = ("ai",)
DISCOVERY_DECISIONS = ("accept", "reject")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


class CompetitorRecord(Base):
    __tablename__ = "competitors"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitors_workspace_id_id"),
        UniqueConstraint("workspace_id", "idempotency_key_hash", name="uq_competitors_idempotency"),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_competitors_workspace_product",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"role IN {_in(ROLES)}", name="role"),
        CheckConstraint(f"status IN {_in(STATUSES)}", name="status"),
        CheckConstraint(f"input_kind IN {_in(INPUT_KINDS)}", name="input_kind"),
        Index("ix_competitors_product", "workspace_id", "product_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: The name a resolve searches; null for a link input.
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Registrable domain of the official site (``acme-charge.example``).
    website_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    input_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The input as pasted (a link or the name).
    input_text: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: The alias of a pasted ``facebook.com/<alias>`` link.
    input_page_alias: Mapped[str | None] = mapped_column(String(100), nullable=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    archived_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompetitorResolutionRecord(Base):
    __tablename__ = "competitor_resolutions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitor_resolutions_workspace_id_id"),
        UniqueConstraint("workspace_id", "job_id", name="uq_competitor_resolutions_job"),
        UniqueConstraint(
            "workspace_id", "competitor_id", "idempotency_key_hash", name="uq_competitor_resolutions_idempotency",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "competitor_id"],
            ["competitors.workspace_id", "competitors.id"],
            name="fk_competitor_resolutions_competitor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["jobs.workspace_id", "jobs.id"],
            name="fk_competitor_resolutions_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_competitor_resolutions_operation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "credential_version_id"],
            ["credential_versions.workspace_id", "credential_versions.id"],
            name="fk_competitor_resolutions_credential_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN {_in(RESOLUTION_STATUSES)}", name="status"),
        CheckConstraint(f"execution_mode IN {_in(EXECUTION_MODES)}", name="execution_mode"),
        CheckConstraint(
            "(execution_mode = 'byok') = (credential_version_id IS NOT NULL)", name="byok_credential",
        ),
        Index("ix_competitor_resolutions_competitor", "workspace_id", "competitor_id", "created_at"),
        Index("ix_competitor_resolutions_credential", "workspace_id", "credential_version_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    competitor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The key admission reserved against (byok); the key each call actually
    #: used is on its ProviderCallAttempt.
    credential_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query: Mapped[str] = mapped_column(String(200), nullable=False)
    website_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    quote_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Search, candidates with scores and probes, waits and call references.
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    cost_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ``finished_at`` + 72 hours: until then a confirmation may cite it.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompetitorIdentityConfirmationRecord(Base):
    __tablename__ = "competitor_identity_confirmations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitor_identity_confirmations_workspace_id_id"),
        UniqueConstraint(
            "workspace_id", "competitor_id", "revision", name="uq_competitor_identity_confirmations_revision",
        ),
        UniqueConstraint(
            "workspace_id", "competitor_id", "idempotency_key_hash",
            name="uq_competitor_identity_confirmations_idempotency",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "competitor_id"],
            ["competitors.workspace_id", "competitors.id"],
            name="fk_competitor_identity_confirmations_competitor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "resolve_job_id"],
            ["competitor_resolutions.workspace_id", "competitor_resolutions.job_id"],
            name="fk_competitor_identity_confirmations_resolution",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint("platform = 'meta'", name="platform"),
        CheckConstraint(f"source IN {_in(CONFIRMATION_SOURCES)}", name="source"),
        CheckConstraint("(source = 'link') = (resolve_job_id IS NULL)", name="source_resolution"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    competitor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, default="meta")
    page_id: Mapped[str] = mapped_column(String(32), nullable=False)
    page_alias: Mapped[str | None] = mapped_column(String(100), nullable=True)
    page_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    resolve_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The link it came from, or the resolve's candidate card and probe for
    #: that page (no avatar), as confirmed.
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: The command's ``Idempotency-Key`` hash; a revision the system appended
    #: (a link on create, an auto match) hashes its own natural key instead.
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CompetitorScanRecord(Base):
    __tablename__ = "competitor_scans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitor_scans_workspace_id_id"),
        UniqueConstraint("workspace_id", "idempotency_key_hash", name="uq_competitor_scans_idempotency"),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_competitor_scans_product",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["jobs.workspace_id", "jobs.id"],
            name="fk_competitor_scans_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_competitor_scans_operation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "initial_credential_version_id"],
            ["credential_versions.workspace_id", "credential_versions.id"],
            name="fk_competitor_scans_credential_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN {_in(SCAN_STATUSES)}", name="status"),
        CheckConstraint(f"stage IS NULL OR stage IN {_in(SCAN_STAGES)}", name="stage"),
        CheckConstraint(f"execution_mode IN {_in(EXECUTION_MODES)}", name="execution_mode"),
        CheckConstraint("platform = 'meta'", name="platform"),
        CheckConstraint(
            "(execution_mode = 'byok') = (initial_credential_version_id IS NOT NULL)", name="byok_credential",
        ),
        Index("ix_competitor_scans_product", "workspace_id", "product_id", "created_at"),
        Index("ix_competitor_scans_status", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The scan's market: an ISO 3166-1 alpha-2 country (``params_json.country``).
    market: Mapped[str] = mapped_column(String(2), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, default="meta")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    #: The last completed step; null until fetch completes.
    stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Competitors, keywords, page limits, LLM caps and the frozen ``language``.
    params_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The key the scan was admitted with; each call's key is on its attempt.
    initial_credential_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The latest admission's Job and Operation (a resume replaces them).
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Admission (quote, reservations), per-step evidence, cursors, waits,
    #: ``resumes[]`` and reconciliation results.
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: ``{"market_data": {...}, "llm": {...}}`` settled so far.
    cost_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: The latest snapshot of this scan (C-23 owns snapshots).
    snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompetitorAdRecord(Base):
    __tablename__ = "competitor_ads"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitor_ads_workspace_id_id"),
        UniqueConstraint("workspace_id", "scan_id", "ad_archive_id", name="uq_competitor_ads_scan_ad"),
        ForeignKeyConstraint(
            ["workspace_id", "scan_id"],
            ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_ads_scan",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    scan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    ad_archive_id: Mapped[str] = mapped_column(String(32), nullable=False)
    collation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    collation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    advertiser_page_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    advertiser_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    page_profile_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    page_categories_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Whole days between ``start_date`` and ``observed_at`` (derived).
    days_running: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    publisher_platforms_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: Card 0 = the snapshot's own creative; text fields and media refs.
    cards_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    landing_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    library_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    #: Each card's image / video poster as observed: the link without its
    #: signature, the field it came from and when the signed link expires.
    #: What archiving found is in the scan's ``evidence_json.media``.
    media_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: ``sha256:<digest of the page response>#<path of the item>``.
    raw_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    field_sources_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CompetitorAdOriginRecord(Base):
    __tablename__ = "competitor_ad_origins"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitor_ad_origins_workspace_id_id"),
        UniqueConstraint("workspace_id", "competitor_ad_id", "origin", name="uq_competitor_ad_origins_origin"),
        ForeignKeyConstraint(
            ["workspace_id", "competitor_ad_id"],
            ["competitor_ads.workspace_id", "competitor_ads.id"],
            name="fk_competitor_ad_origins_ad",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scan_id"],
            ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_ad_origins_scan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["provider_call_attempts.workspace_id", "provider_call_attempts.id"],
            name="fk_competitor_ad_origins_attempt",
            ondelete="RESTRICT",
        ),
        Index("ix_competitor_ad_origins_scan", "workspace_id", "scan_id", "origin"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    competitor_ad_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``page:<competitor_id>`` or ``keyword:<keyword>``.
    origin: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The page call that saw it.
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CompetitorAdAssessmentRecord(Base):
    __tablename__ = "competitor_ad_assessments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitor_ad_assessments_workspace_id_id"),
        UniqueConstraint(
            "workspace_id", "competitor_ad_id", "kind", "revision", name="uq_competitor_ad_assessments_revision",
        ),
        UniqueConstraint(
            "workspace_id", "competitor_ad_id", "kind", "idempotency_key_hash",
            name="uq_competitor_ad_assessments_idempotency",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "competitor_ad_id"],
            ["competitor_ads.workspace_id", "competitor_ads.id"],
            name="fk_competitor_ad_assessments_ad",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scan_id"],
            ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_ad_assessments_scan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["provider_call_attempts.workspace_id", "provider_call_attempts.id"],
            name="fk_competitor_ad_assessments_attempt",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint(f"kind IN {_in(ASSESSMENT_KINDS)}", name="kind"),
        CheckConstraint(f"source IN {_in(ASSESSMENT_SOURCES)}", name="source"),
        CheckConstraint(f"relevance IS NULL OR relevance IN {_in(RELEVANCE_VALUES)}", name="relevance"),
        CheckConstraint("(kind = 'relevance') = (relevance IS NOT NULL)", name="relevance_kind"),
        CheckConstraint("(source = 'ai') = (attempt_id IS NOT NULL)", name="ai_attempt"),
        Index("ix_competitor_ad_assessments_scan", "workspace_id", "scan_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    scan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    competitor_ad_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 1, 2, ... per (observation, kind); the highest is in effect.
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ``ai`` (an ``llm_chat`` answer), ``rule`` (§7.1 rules, no LLM) or ``user`` (a flip).
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    #: ``relevant`` | ``adjacent`` | ``unrelated`` for ``kind='relevance'`` (also in the payload).
    relevance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The judgment: confidence and one-sentence reason in the scan's language
    #: (ai), the rule that matched (rule), or what the flip replaced (user).
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Which call answered it: Job, call name, model, prompt version, batch item.
    model_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: The ``llm_chat`` attempt behind an ``ai`` revision.
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: A flip's ``Idempotency-Key`` hash; a revision the Worker appended hashes
    #: its own natural key instead (so a redelivered Job never appends twice).
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The member who flipped it; null for the Worker's revisions.
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CompetitorScanAssessmentRecord(Base):
    __tablename__ = "competitor_scan_assessments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitor_scan_assessments_workspace_id_id"),
        UniqueConstraint("workspace_id", "scan_id", "kind", "revision", name="uq_competitor_scan_assessments_revision"),
        UniqueConstraint(
            "workspace_id", "scan_id", "kind", "idempotency_key_hash",
            name="uq_competitor_scan_assessments_idempotency",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scan_id"],
            ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_scan_assessments_scan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["jobs.workspace_id", "jobs.id"],
            name="fk_competitor_scan_assessments_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["provider_call_attempts.workspace_id", "provider_call_attempts.id"],
            name="fk_competitor_scan_assessments_attempt",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint(f"kind IN {_in(SCAN_ASSESSMENT_KINDS)}", name="kind"),
        CheckConstraint(f"source IN {_in(SCAN_ASSESSMENT_SOURCES)}", name="source"),
        CheckConstraint("(source = 'ai') = (attempt_id IS NOT NULL)", name="ai_attempt"),
        Index("ix_competitor_scan_assessments_scan", "workspace_id", "scan_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    scan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 1, 2, ... per (scan, kind); the highest is in effect.
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ``ai``: an ``llm_chat`` answer (the cluster step or a paid recluster).
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    #: ``claim_clusters`` (each: ID, one-sentence claim, advertisers with
    #: ``role``, observation IDs, longest days running), the observations it
    #: was asked about, the one-sentence conclusion and the scan's language.
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Which call answered it: Job, call name, model, prompt version.
    model_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: The Job that ran the call: the scan's own run or a recluster's.
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The natural key of the call that produced it (a redelivered Job never appends twice).
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The member who asked for the recluster; null for the scan's own step.
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CompetitorScanSnapshotRecord(Base):
    __tablename__ = "competitor_scan_snapshots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitor_scan_snapshots_workspace_id_id"),
        UniqueConstraint("workspace_id", "scan_id", "hash", name="uq_competitor_scan_snapshots_hash"),
        ForeignKeyConstraint(
            ["workspace_id", "scan_id"],
            ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_scan_snapshots_scan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_competitor_scan_snapshots_product",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "cluster_assessment_id"],
            ["competitor_scan_assessments.workspace_id", "competitor_scan_assessments.id"],
            name="fk_competitor_scan_snapshots_clusters",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"execution_mode IN {_in(EXECUTION_MODES)}", name="execution_mode"),
        CheckConstraint("(cluster_revision IS NULL) = (cluster_assessment_id IS NULL)", name="cluster_reference"),
        Index("ix_competitor_scan_snapshots_market", "workspace_id", "product_id", "market", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    scan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The scan's product and market, copied for "the latest snapshot of this
    #: product in this market" (spec §7.2) without reading the scan.
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(2), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The clusters revision it cites (null when the scan has none yet).
    cluster_assessment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cluster_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: SHA-256 of the canonical ``summary_json``: equal revisions, equal hash.
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The frozen content (spec §7.1); the hash covers all of it.
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Who froze it: the scan Job (``job_id``) or a member's refreeze (``created_by``).
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CompetitorDiscoveryDecisionRecord(Base):
    __tablename__ = "competitor_discovery_decisions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_competitor_discovery_decisions_workspace_id_id"),
        UniqueConstraint(
            "workspace_id", "product_id", "idempotency_key_hash", name="uq_competitor_discovery_decisions_idempotency",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_competitor_discovery_decisions_product",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scan_id"],
            ["competitor_scans.workspace_id", "competitor_scans.id"],
            name="fk_competitor_discovery_decisions_scan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "competitor_id"],
            ["competitors.workspace_id", "competitors.id"],
            name="fk_competitor_discovery_decisions_competitor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"decision IN {_in(DISCOVERY_DECISIONS)}", name="decision"),
        CheckConstraint("(decision = 'accept') = (competitor_id IS NOT NULL)", name="accept_competitor"),
        Index(
            "ix_competitor_discovery_decisions_page", "workspace_id", "product_id", "advertiser_page_id", "decided_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    #: Decisions are per product: a rejection keeps the advertiser out of the
    #: product's later scans' recommendations.
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The scan whose recommendation was decided on.
    scan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The advertiser's Meta page ID, as its observations carry it.
    advertiser_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    advertiser_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: ``accept`` | ``reject``.
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The member's optional reason.
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: The competitor an accept created (confirmed by the page ID from the ads).
    competitor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The recommendation as decided on: the discover call, its judgment and counts.
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
