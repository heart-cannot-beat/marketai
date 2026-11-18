"""User-attested real product photos, resolved through governed Library versions.

A released reference only proves file/rights gates. The separate selection
records who attested its product identity and source; generated ads are never
promoted to product truth. Frozen hashes are checked again before dispatch.
Without an explicit choice, a new draft freezes the product/model's verified
main and detail photos. Logos and certification originals never stand in for
the physical product, and a photo only supports visible appearance.

Ads-first ideas (ADR 0018 §2, #199) may also draft with photos nobody checked
yet: the released candidates the product's reading found. They are frozen by
the content hash of the derivative the provider reads, marked
``unverified_product_photo``, and only a draft may use them: an ad made with
one is exportable once the user checks that photo, and needs making again once
the user rejects it (``ideas_export``). Plan-first drafts never get one.
"""
from dataclasses import asdict
import base64
from sqlalchemy import select
from app.adapters.persistence.models import (LibraryEntryRecord, LibraryVersionRecord, ProductIntakeRecord, ProductRecord,
                                            ReferenceObjectRecord)
from app.generation.creative_recipe import committed_key_in_workspace
from app.platform.blob_store import CommittedBlobRef
from .service import MarketingError


from .contracts import ProductReferenceSelection

MAX_REFERENCES = 3
SET_POLICY = "product-reference-set-v1"
APPEARANCE_ROLES = ("primary", "detail")  # freezing order; never logo or certification_mark
REFERENCE_BRIEF = "Match the product's appearance exactly to the reference photos; do not invent ports, labels or colors."
#: An ads-first draft's photo from the product's reading that the user has not checked (#199).
IDEAS_POLICY = "ads-first-unverified-photos-v1"
UNVERIFIED = "unverified_product_photo"
#: Roles the reading gives a photo that shows the product's look; packaging, scenes in use and the rest
#: never stand in for it. The first ``main`` photo is the main one.
CANDIDATE_ROLES = ("main", "angle", "detail")


def _blob(obj) -> dict:
    return asdict(CommittedBlobRef(committed_key=obj.derivative_object_key, storage_version_id=obj.derivative_storage_version_id or "",
        size_bytes=int(obj.derivative_byte_size or 0), checksum_sha256_b64=base64.b64encode(bytes.fromhex(obj.derivative_sha256)).decode(),
        content_type_verified=obj.derivative_mime or "image/jpeg"))


def _usable(obj, ctx) -> bool:
    return bool(obj and obj.state == "released" and committed_key_in_workspace(obj.derivative_object_key or "", ctx.workspace_id)
                and obj.derivative_sha256)


def _gated(session, ctx, object_id):
    """A reading's photo as it is now: the released derivative of a reference object of this Workspace."""
    obj = session.scalar(select(ReferenceObjectRecord).where(ReferenceObjectRecord.workspace_id == ctx.workspace_id,
        ReferenceObjectRecord.id == object_id)) if object_id else None
    if not _usable(obj, ctx):
        raise MarketingError("product_reference_unavailable", "参考图不存在、尚未通过文件/权利检查或已撤回，请重新选择。")
    # No creative-library version yet: ``content_hash`` is the original file's hash, and the object id
    # labels the photo in a check's input where a library photo has its version id.
    return {"reference_version_id": None, "reference_object_id": obj.id, "content_hash": obj.content_sha256,
            "role": "product_reference", "asset_version_id": obj.id, "content_sha256": obj.derivative_sha256,
            "blob": _blob(obj)}


def _live(session, ctx, ref):
    """What a frozen photo's file identity is now: a library reference's, or a reading's photo's."""
    if ref.get("selection_policy") == IDEAS_POLICY:
        return _gated(session, ctx, ref.get("reference_object_id"))
    return _released(session, ctx, ref["reference_version_id"])


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
    if not _usable(obj, ctx):
        raise MarketingError("product_reference_unavailable", "参考图不存在、尚未通过文件/权利检查或已撤回，请重新选择。")
    return {"reference_version_id": version_id, "content_hash": pair[0].content_hash,
        "role": "product_reference", "asset_version_id": version_id, "content_sha256": obj.derivative_sha256,
        "blob": _blob(obj)}


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


def candidates(session, ctx, project) -> list[dict]:
    """The photos the reading of an ads-first project found that may show the product's look (#199).

    The released candidates of its first batch's product intake -- the images that passed the reference
    gate -- with a ``main``, ``angle`` or ``detail`` role, of the model the user chose, in the page's
    order. A photo is its derivative's content hash (``photo_id``): the bytes a provider reads, so the
    same file found twice is one photo. A project no first batch opened has none."""
    from .models import MarketingFirstBatchRecord
    batch = session.scalar(select(MarketingFirstBatchRecord).where(
        MarketingFirstBatchRecord.workspace_id == ctx.workspace_id, MarketingFirstBatchRecord.project_id == project.id)
        .order_by(MarketingFirstBatchRecord.created_at, MarketingFirstBatchRecord.id).limit(1))
    intake = session.scalar(select(ProductIntakeRecord).where(ProductIntakeRecord.workspace_id == ctx.workspace_id,
        ProductIntakeRecord.id == batch.intake_id)) if batch is not None else None
    if intake is None:
        return []
    images = sorted((item for item in intake.images_json or [] if "rank" in item and item.get("gate_state") == "released"
                     and item.get("reference_object_id") and item.get("role") in CANDIDATE_ROLES), key=lambda item: item["rank"])
    variants = [v for v in (intake.card_json or {}).get("model_variants") or [] if isinstance(v, dict)]
    if variants and batch.selected_variant:
        from app.products.intake_extraction import variant_image_ids
        kept = set(variant_image_ids(variants, batch.selected_variant, [item["image_id"] for item in images]))
        images = [item for item in images if item["image_id"] in kept]
    objects = {obj.id: obj for obj in session.scalars(select(ReferenceObjectRecord).where(
        ReferenceObjectRecord.workspace_id == ctx.workspace_id,
        ReferenceObjectRecord.id.in_([item["reference_object_id"] for item in images]))).all()} if images else {}
    found, seen = [], set()
    for item in images:
        obj = objects.get(item["reference_object_id"])
        if not _usable(obj, ctx) or obj.derivative_sha256 in seen:
            continue
        seen.add(obj.derivative_sha256)
        uploaded = item.get("source") == "upload"
        found.append({"photo_id": obj.derivative_sha256, "reference_object_id": obj.id, "intake_id": intake.id,
                      "image_id": item["image_id"], "role": item["role"], "rank": item["rank"],
                      "source": "upload" if uploaded else "product_page",
                      "source_url": None if uploaded else (item.get("source_url") or intake.source_url),
                      "width": obj.derivative_width, "height": obj.derivative_height,
                      # What a check of the photo declares its rights from (``intake_service._rights_for``).
                      "intake_image": dict(item), "page_url": intake.source_url})
    return found


def for_ideas(session, ctx, project, states=None) -> list[dict]:
    """The product photos a new image of ads-first ideas is drafted with (#199, design doc §5.2).

    A main photo and at most two more, from the reading's photos (``candidates``) and the product's
    verified photos (``from_product_set``), one per content hash. A photo the user rejected is never used,
    nor one checked whose creative-library reference can no longer be used; checked photos come first, then unchecked ones, each in the page's order. The main photo is the first
    the reading called ``main`` (or the product's ``primary``), else the first one. A photo the product's
    set lists as verified is frozen as that creative-library reference; any other as the reading's photo,
    ``unverified_product_photo``. Without any reading's photo this is ``from_product_set``, as before.

    ``states``: each photo's ``verified`` / ``rejected`` / ``unverified`` (``ideas_export.photo_states``)."""
    verified = from_product_set(session, ctx, project)
    found = candidates(session, ctx, project)
    if not found:
        return verified
    if states is None:
        from .ideas_export import photo_states
        states = photo_states(session, ctx, project, found, verified)
    library = {ref["content_sha256"]: ref for ref in verified}
    pool = [(photo["photo_id"], photo["role"] == "main", photo) for photo in found]
    pool += [(ref["content_sha256"], ref["reference_role"] == "primary", None) for ref in verified
             if ref["content_sha256"] not in {photo["photo_id"] for photo in found}]
    usable = [entry for entry in pool if states.get(entry[0]) not in ("rejected", "unavailable")]
    ordered = [entry for entry in usable if states.get(entry[0]) == "verified"] + \
              [entry for entry in usable if states.get(entry[0]) != "verified"]
    if not ordered:
        return []
    main = next((entry for entry in ordered if entry[1]), ordered[0])
    result = []
    for photo_id, _, photo in [main, *(entry for entry in ordered if entry is not main)]:
        if len(result) == MAX_REFERENCES:
            break
        if photo_id in library:
            result.append(library[photo_id])
            continue
        try:
            live = _gated(session, ctx, photo["reference_object_id"])
        except MarketingError:
            continue
        result.append({**live, "source": f"product_intake:{photo['intake_id']}:{photo['image_id']}",
                       "attestation": UNVERIFIED, "selection_policy": IDEAS_POLICY, "product_id": project.product_id,
                       "intake_id": photo["intake_id"], "image_id": photo["image_id"],
                       "reference_role": "primary" if not result else "detail"})
    return result


def _current(session, ctx, frozen):
    """Each frozen photo's live file/rights identity; its attestation and selection provenance stay as frozen."""
    return [{**ref, **_live(session, ctx, ref)} for ref in frozen]


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
    """Released/rights state remains a live gate after generation as well.

    A photo nobody had checked when the image was made (#199) is never current here: rounds, reuse and
    their checks accept an image only on photos that were creative-library verified references then. An
    ads-first ad answers its photos' checking through ``ideas_export`` instead."""
    if (lineage or {}).get("policy") != "marketing-product-photos-v1":
        return True
    refs = (lineage or {}).get("references", [])
    if not refs:
        return True
    if any(ref.get("attestation") == UNVERIFIED or not ref.get("reference_version_id") for ref in refs):
        return False
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
