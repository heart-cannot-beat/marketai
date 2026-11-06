"""User-attested real product photos, resolved through governed Library versions.

A released reference only proves file/rights gates. The separate selection
records who attested its product identity and source; generated ads are never
promoted to product truth. Frozen hashes are checked again before dispatch.
Without an explicit choice, a new draft freezes the product/model's verified
main and detail photos. Logos and certification originals never stand in for
the physical product, and a photo only supports visible appearance.
"""
from dataclasses import asdict
import base64
from sqlalchemy import select
from app.adapters.persistence.models import LibraryEntryRecord, LibraryVersionRecord, ProductRecord, ReferenceObjectRecord
from app.generation.creative_recipe import committed_key_in_workspace
from app.platform.blob_store import CommittedBlobRef
from .service import MarketingError


from .contracts import ProductReferenceSelection

MAX_REFERENCES = 3
SET_POLICY = "product-reference-set-v1"
APPEARANCE_ROLES = ("primary", "detail")  # freezing order; never logo or certification_mark
REFERENCE_BRIEF = "Match the product's appearance exactly to the reference photos; do not invent ports, labels or colors."


def _released(session, ctx, version_id):
    pair = session.execute(select(LibraryVersionRecord, LibraryEntryRecord).join(LibraryEntryRecord,
        (LibraryEntryRecord.id == LibraryVersionRecord.entry_id) & (LibraryEntryRecord.workspace_id == ctx.workspace_id))
        .where(LibraryVersionRecord.workspace_id == ctx.workspace_id, LibraryVersionRecord.id == version_id,
            LibraryVersionRecord.entry_type == "reference_asset", LibraryVersionRecord.state == "effective",
            LibraryVersionRecord.eligibility != "blocked", LibraryEntryRecord.lifecycle == "active")).first()
    obj = None
    from app.library.contracts import check_rights_eligible
    if pair and check_rights_eligible((pair[0].content_json or {}).get("rights") or {}):
        pair = None
    if pair:
        obj = session.scalar(select(ReferenceObjectRecord).where(ReferenceObjectRecord.workspace_id == ctx.workspace_id,
            ReferenceObjectRecord.id == (pair[0].content_json or {}).get("object_id")))
    if not obj or obj.state != "released" or not committed_key_in_workspace(obj.derivative_object_key or "", ctx.workspace_id) or not obj.derivative_sha256:
        raise MarketingError("product_reference_unavailable", "参考图不存在、尚未通过文件/权利检查或已撤回，请重新选择。")
    blob = CommittedBlobRef(committed_key=obj.derivative_object_key, storage_version_id=obj.derivative_storage_version_id or "",
        size_bytes=int(obj.derivative_byte_size or 0), checksum_sha256_b64=base64.b64encode(bytes.fromhex(obj.derivative_sha256)).decode(),
        content_type_verified=obj.derivative_mime or "image/jpeg")
    return {"reference_version_id": version_id, "content_hash": pair[0].content_hash,
        "role": "product_reference", "asset_version_id": version_id, "content_sha256": obj.derivative_sha256,
        "blob": asdict(blob)}


def resolve(session, ctx, selection):
    selection = ProductReferenceSelection.model_validate(selection or {})
    ids = selection.reference_version_ids
    if not ids:
        return []
    if len(set(ids)) != len(ids) or not selection.confirms_real_product or not selection.source.strip():
        raise MarketingError("product_reference_confirmation_required", "请选择不重复的实物参考图，并确认产品身份及来源。")
    return [{**_released(session, ctx, version_id), "source": selection.source, "attestation": "user_confirmed_real_product"}
        for version_id in ids]


def source_product(session, ctx, project):
    """The Library-profiled product this test started from; revising the source text drops that link."""
    from .models import MarketingRevisionRecord as Revision
    scope = (Revision.workspace_id == ctx.workspace_id, Revision.project_id == project.id)
    chosen = (session.scalar(select(Revision.content_json).where(*scope, Revision.revision == 1)) or {}).get("product_id")
    if not chosen or session.scalar(select(Revision.id).where(*scope, Revision.reason == "revise_source").limit(1)):
        return None
    return session.scalar(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
        ProductRecord.product_profile_entry_id.is_not(None),
        (ProductRecord.id == chosen) | (ProductRecord.product_profile_entry_id == chosen)).limit(1))


def from_product_set(session, ctx, project):
    """Verified main/detail photos of the exact product model, frozen by hash for a new draft.

    A product with several models never shares photos automatically, and an
    unavailable photo is skipped; without any the round stays pending verification.
    """
    from app.products.reference_sets import published
    product = source_product(session, ctx, project)
    current = published(session, ctx, product) if product else None
    if not current or len(current["model_variants"]) > 1:
        return []
    model = current["model_variants"][0] if current["model_variants"] else ""
    eligible = [item for item in current["items"] if item["role"] in APPEARANCE_ROLES and item["model_variant"] == model
        and item["identity_status"] == "verified_physical" and item["available"]]
    result = []
    for item in sorted(eligible, key=lambda item: APPEARANCE_ROLES.index(item["role"])):
        if len(result) == MAX_REFERENCES:
            break
        if any(ref["reference_version_id"] == item["reference_version_id"] for ref in result):
            continue
        try:
            released = _released(session, ctx, item["reference_version_id"])
        except MarketingError:
            continue
        result.append({**released, "source": item["note"].strip() or "Product reference settings",
            "attestation": "verified_physical_product", "selection_policy": SET_POLICY, "product_id": product.id,
            "product_version_id": current["version_id"], "item_id": item["item_id"], "model_variant": model,
            "reference_role": item["role"]})
    return result


def _current(session, ctx, frozen):
    """Each frozen photo's live file/rights identity; its attestation and selection provenance stay as frozen."""
    return [{**ref, **_released(session, ctx, ref["reference_version_id"])} for ref in frozen]


def assert_current(session, ctx, frozen):
    if not frozen:
        return
    if _current(session, ctx, frozen) != frozen:
        raise MarketingError("product_reference_changed", "实物参考图或其权利状态已变化，请重新预览。", 409)


def references_current(session, ctx, frozen):
    try:
        assert_current(session, ctx, frozen)
    except MarketingError:
        return False
    return True


def lineage_current(session, ctx, lineage):
    """Released/rights state remains a live gate after generation as well."""
    if (lineage or {}).get("policy") != "marketing-product-photos-v1":
        return True
    refs = (lineage or {}).get("references", [])
    if not refs:
        return True
    try:
        return all(all(old.get(k) == new[k] for k in ("reference_version_id", "content_sha256", "content_hash"))
            for old, new in ((ref, _released(session, ctx, ref["reference_version_id"])) for ref in refs))
    except MarketingError:
        return False


def appearance(session, ctx, frozen, version=None):
    """Board status mirroring `ai_qc.frozen_input`: appearance is checkable only against current verified photos."""
    refs = frozen or (lineage_references(version) if version is not None else [])
    current = bool(refs) and (references_current(session, ctx, frozen) if frozen
        else lineage_current(session, ctx, version.reference_sources_json))
    return {"status": "reference_backed" if current else "pending_verification",
        "reason": None if current else "reference_not_current" if refs else "no_reference",
        "reference_version_ids": [ref["reference_version_id"] for ref in refs]}


def lineage_references(version):
    lineage = version.reference_sources_json or {}
    return lineage.get("references", []) if lineage.get("policy") == "marketing-product-photos-v1" else []


def for_asset(session, ctx, version):
    """Reused creatives retain the same authoritative photo identity, hash and attestation."""
    refs = lineage_references(version)
    if not refs:
        return []
    if not lineage_current(session, ctx, version.reference_sources_json):
        raise MarketingError("product_reference_changed", "原素材的实物参考或权利已不可用，请核对后重新生成。", 409)
    return _current(session, ctx, refs)
