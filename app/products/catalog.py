"""The product list the front flow reads (Issue #162).

Front-flow contract §1 principle 2: the user never sees ``prod_``/``lib_``
identifiers. Every page that used to print a ``prod_`` id (product home,
Generation drafts/runs, the New Draft product picker) needs the product's
display name, its main image and whether it can be used for a new draft.
The Library list (``list_library_entries``) carries none of the ``prod_``
bridge, the published card or its bound images, so this read projects them
in one Workspace-scoped query instead of N+1 browser reads.

It is a projection only: names come from the effective ProductVersion
(falling back to the entry and then the bridged ``products`` row), the main
image is the first-priority reference binding of that version, and the
latest Generation Run is summarized without ids beyond the run itself.
"""
from __future__ import annotations

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    ProductRecord,
)
from app.library.contracts import ENTRY_PRODUCT, LIFECYCLE_ARCHIVED, VERSION_EFFECTIVE
from app.platform.tenancy import TenantContext

#: The page shows a product home, not an export; a Workspace with more
#: products than this is out of the invitation-only Beta's scale.
MAX_PRODUCTS = 500


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _primary_reference(content: dict) -> str | None:
    """The first-priority reference binding's version id (intake binds its
    chosen main image at priority 0; Library-made products bind as ``hero``
    or ``product_reference`` with their own priorities)."""
    bindings = [
        item for item in (content.get("reference_bindings") or [])
        if isinstance(item, dict) and item.get("reference_version_id")
    ]
    if not bindings:
        return None
    ordered = sorted(enumerate(bindings), key=lambda pair: (int(pair[1].get("priority") or 0), pair[0]))
    return str(ordered[0][1]["reference_version_id"])


def _variant_label(content: dict) -> str | None:
    """The confirmed model when the card carries exactly one variant."""
    variants = [
        item for item in (content.get("model_variants") or [])
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]
    if len(variants) == 1:
        return str(variants[0]["label"]).strip()
    return None


def _marketing_anchors(session, ctx: TenantContext) -> dict[str, str]:
    """Product anchors marketing tests minted, mapped to the product each test started from.

    Confirming a test's understanding freezes it as its own ``products`` row
    (``marketing.service.edit_project``). When the test started from a product
    card, that row is the same product in the test's own wording, not another
    product for the home page (J-32). A test whose source text was revised no
    longer stands for its card (the same rule as its product photos).
    """
    from app.marketing.models import MarketingRevisionRecord as Revision

    chosen = Revision.content_json["product_id"].as_string()
    rows = session.execute(
        select(Revision.project_id, Revision.revision, Revision.reason, chosen).where(
            Revision.workspace_id == ctx.workspace_id,
            Revision.reason.in_(("created", "confirm_understanding", "revise_source")),
        )
    ).all()
    revised = {project for project, _, reason, _ in rows if reason == "revise_source"}
    sources = {
        project: product for project, revision, reason, product in rows
        if reason == "created" and revision == 1 and product and project not in revised
    }
    return {
        product: sources[project] for project, _, reason, product in rows
        if reason == "confirm_understanding" and product and project in sources and product != sources[project]
    }


def list_products(ctx: TenantContext) -> list[dict]:
    """Every non-archived product of the Workspace, most recently changed first."""
    with tenant_session(ctx) as session:
        rows = session.execute(
            select(ProductRecord, LibraryEntryRecord)
            .outerjoin(
                LibraryEntryRecord,
                (LibraryEntryRecord.id == ProductRecord.product_profile_entry_id)
                & (LibraryEntryRecord.workspace_id == ProductRecord.workspace_id)
                & (LibraryEntryRecord.entry_type == ENTRY_PRODUCT),
            )
            .where(ProductRecord.workspace_id == ctx.workspace_id)
            .limit(MAX_PRODUCTS)
        ).all()

        version_ids = [
            entry.current_version_id for _, entry in rows
            if entry is not None and entry.current_version_id
        ]
        versions: dict[str, LibraryVersionRecord] = {}
        if version_ids:
            for version in session.scalars(
                select(LibraryVersionRecord).where(
                    LibraryVersionRecord.workspace_id == ctx.workspace_id,
                    LibraryVersionRecord.id.in_(version_ids),
                )
            ).all():
                versions[version.id] = version

        # A test's anchor stays out of the list; its Runs count for the card it started from.
        by_entry = {entry.id: product.id for product, entry in rows if entry is not None}
        listed = {product.id for product, _ in rows}
        anchored: dict[str, list[str]] = {}
        for anchor, source in _marketing_anchors(session, ctx).items():
            owner = by_entry.get(source, source)
            if anchor in listed and owner in listed and owner != anchor:
                anchored.setdefault(owner, []).append(anchor)
        # Older tests reused a Library entry's id as a separate compatibility row.
        for product, _ in rows:
            if product.product_profile_entry_id is None and product.id in by_entry:
                anchored.setdefault(by_entry[product.id], []).append(product.id)
        hidden = {anchor for anchors in anchored.values() for anchor in anchors}

        product_ids = [product.id for product, _ in rows]
        latest_runs: dict[str, GenerationRunRecord] = {}
        if product_ids:
            for run in session.scalars(
                select(GenerationRunRecord)
                .where(
                    GenerationRunRecord.workspace_id == ctx.workspace_id,
                    GenerationRunRecord.product_id.in_(product_ids),
                )
                .order_by(GenerationRunRecord.created_at.desc(), GenerationRunRecord.id.desc())
            ).all():
                latest_runs.setdefault(run.product_id, run)

        items: list[dict] = []
        for product, entry in rows:
            if product.id in hidden or (entry is not None and entry.lifecycle == LIFECYCLE_ARCHIVED):
                continue
            version = versions.get(entry.current_version_id) if entry is not None and entry.current_version_id else None
            if version is not None and version.state != VERSION_EFFECTIVE:
                version = None
            content = dict(version.content_json or {}) if version is not None else {}
            name = (
                str(content.get("name") or "").strip()
                or (entry.display_name.strip() if entry is not None else "")
                or (product.name or "").strip()
                or None
            )
            run = max(
                (latest_runs[pid] for pid in [product.id, *anchored.get(product.id, [])] if pid in latest_runs),
                key=lambda candidate: (candidate.created_at, candidate.id),
                default=None,
            )
            updated = max(
                (moment for moment in (
                    product.updated_at,
                    entry.updated_at if entry is not None else None,
                ) if moment is not None),
                default=None,
            )
            items.append({
                "product_id": product.id,
                "entry_id": entry.id if entry is not None else None,
                "name": name,
                "variant_label": _variant_label(content),
                # A Library-profiled product is usable once it has an effective
                # published version; a legacy product without a profile always is.
                "ready": version is not None if entry is not None else True,
                "primary_reference_version_id": _primary_reference(content),
                "latest_run": {
                    "status": run.status,
                    "done": int(run.done or 0),
                    "total": int(run.total or 0),
                    "failed": int(run.failed or 0),
                    "created_at": _iso(run.created_at),
                } if run is not None else None,
                "created_at": _iso(product.created_at),
                "updated_at": _iso(updated),
            })
        items.sort(key=lambda item: (item["updated_at"] or "", item["product_id"]), reverse=True)
        return items
