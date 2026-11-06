"""GenerationDraft CRUD (Issue #21 Phase 2).

A Draft is the mutable, per-Workspace editing surface a user shapes before
freezing it into a ResolutionPreview. Every mutation is optimistic-locked on
``revision`` with a real conditional ``UPDATE ... WHERE revision = expected``
(never a bare ORM attribute mutation) -- the same CAS pattern
``app.generation.provider_attempt._advance_projection`` uses for
ProviderAttemptProjection, so two concurrent writers can never silently
clobber each other.
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import ProductRecord, new_id, utcnow
from app.generation.models import GenerationDraftRecord
from app.platform.tenancy import TenantContext


class StaleRevisionError(ValueError):
    """Raised when a CAS UPDATE on a GenerationDraft affects no row."""


class DraftNotFoundError(ValueError):
    """Raised when a Draft id does not resolve inside the caller's Workspace."""


#: Public kwarg name -> ORM column name, for the fields whose Python-facing
#: name differs from the mapped column (the ``_json`` suffix is a storage
#: detail callers should not have to know).
_FIELD_ALIASES = {
    "combination_strategy": "combination_strategy_json",
    "selectors": "selectors_json",
    "format_config": "format_json",
    "exclusions": "exclusions_json",
}

#: Columns `update_draft` is permitted to touch. Deliberately excludes
#: identity/lifecycle columns (`id`, `workspace_id`, `created_by`,
#: `created_at`, `revision`, `state`, `archived_at`) -- those are either
#: immutable or owned by a dedicated transition (`archive_draft`).
_MUTABLE_DRAFT_COLUMNS = {
    "product_id",
    "provider",
    "model",
    "execution_mode",
    "combination_strategy_json",
    "selectors_json",
    "format_json",
    "exclusions_json",
    "regeneration_intent",
    "source_asset_version_id",
    "source_recipe_identity",
}


def create_draft(
    session: Session,
    ctx: TenantContext,
    *,
    product_id: str,
    provider: str,
    model: str,
    execution_mode: str,
    combination_strategy: dict,
    format_config: dict | None = None,
    exclusions: dict | None = None,
    selectors: dict | None = None,
    regeneration_intent: str = "new_generation",
    source_asset_version_id: str | None = None,
    source_recipe_identity: str | None = None,
) -> GenerationDraftRecord:
    """Create a new editable Draft at revision 1.

    ``regeneration_intent``/``source_asset_version_id``/``source_recipe_identity``
    default to a fresh "new_generation" draft; Regeneration (#21 Task 5) calls
    this with those set to link the draft back to the Asset it regenerates.
    """
    # Workspace UI identifies products by their Library ProductProfile ID.
    # Preserve compatibility with callers that already hold the legacy
    # Product ID, while resolving the stable Library identity through the
    # explicit products.product_profile_entry_id bridge for normal UI flows.
    product = session.scalar(
        select(ProductRecord).where(
            ProductRecord.workspace_id == ctx.workspace_id,
            (
                (ProductRecord.id == product_id)
                | (ProductRecord.product_profile_entry_id == product_id)
            ),
        )
    )
    resolved_product_id = product.id if product is not None else product_id

    now = utcnow()
    draft = GenerationDraftRecord(
        id=new_id("gd"),
        workspace_id=ctx.workspace_id,
        product_id=resolved_product_id,
        selectors_json=selectors or {},
        provider=provider,
        model=model,
        execution_mode=execution_mode,
        combination_strategy_json=combination_strategy,
        format_json=format_config or {},
        exclusions_json=exclusions or {},
        regeneration_intent=regeneration_intent,
        source_asset_version_id=source_asset_version_id,
        source_recipe_identity=source_recipe_identity,
        revision=1,
        state="editing",
        created_by=ctx.initiator_id or ctx.executor,
        created_at=now,
        updated_at=now,
    )
    session.add(draft)
    session.flush()
    return draft


def update_draft(
    session: Session,
    ctx: TenantContext,
    draft_id: str,
    *,
    expected_revision: int,
    **fields,
) -> GenerationDraftRecord:
    """CAS-update a Draft's editable fields, advancing ``revision`` by one."""
    draft = _get_draft(session, ctx, draft_id)

    values: dict = {}
    for key, value in fields.items():
        column = _FIELD_ALIASES.get(key, key)
        if column not in _MUTABLE_DRAFT_COLUMNS:
            raise ValueError(f"unknown or immutable draft field: {key!r}")
        values[column] = value
    values["revision"] = expected_revision + 1
    values["updated_at"] = utcnow()

    result = session.execute(
        update(GenerationDraftRecord)
        .where(
            GenerationDraftRecord.id == draft_id,
            GenerationDraftRecord.workspace_id == ctx.workspace_id,
            GenerationDraftRecord.revision == expected_revision,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise StaleRevisionError(
            f"Draft {draft_id} expected revision {expected_revision}, was concurrently modified"
        )
    session.flush()
    session.refresh(draft)
    return draft


def archive_draft(
    session: Session,
    ctx: TenantContext,
    draft_id: str,
    expected_revision: int,
) -> None:
    """CAS-transition a Draft to its terminal ``archived`` state."""
    draft = _get_draft(session, ctx, draft_id)
    now = utcnow()

    result = session.execute(
        update(GenerationDraftRecord)
        .where(
            GenerationDraftRecord.id == draft_id,
            GenerationDraftRecord.workspace_id == ctx.workspace_id,
            GenerationDraftRecord.revision == expected_revision,
        )
        .values(state="archived", archived_at=now, revision=expected_revision + 1, updated_at=now)
    )
    if result.rowcount != 1:
        raise StaleRevisionError(
            f"Draft {draft_id} expected revision {expected_revision}, was concurrently modified"
        )
    session.flush()
    session.refresh(draft)


def get_draft(
    session: Session,
    ctx: TenantContext,
    draft_id: str,
) -> GenerationDraftRecord:
    return _get_draft(session, ctx, draft_id)


def _get_draft(
    session: Session,
    ctx: TenantContext,
    draft_id: str,
) -> GenerationDraftRecord:
    draft = session.execute(
        select(GenerationDraftRecord).where(
            GenerationDraftRecord.id == draft_id,
            GenerationDraftRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if draft is None:
        raise DraftNotFoundError(f"Draft {draft_id} not found in workspace")
    return draft
