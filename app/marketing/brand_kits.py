"""Versioned product brand baselines (spec §6.1, ADR 0013).

Every save appends an immutable version. A plan references the exact version
it was confirmed with, and generation and QC load that version by id and
hash, so editing a product's current kit never rewrites an earlier plan,
recipe or QC input. The logo is one of the product's own role=logo reference
items (#226), frozen by its Library version and derivative hash; its file and
rights gates stay live at every later use.
"""
from __future__ import annotations

import base64
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.adapters.persistence.models import LibraryVersionRecord, ProductRecord, ReferenceObjectRecord, new_id
from app.generation.creative_recipe import committed_key_in_workspace
from app.platform.audit import append_event
from app.platform.blob_store import CommittedBlobRef
from .contracts import BrandKitContent, BrandKitRef
from .models import MarketingRevisionRecord, ProductBrandKitRecord
from .service import MarketingError, digest, ensure_active

PLACEMENTS = {"top_left": "top-left corner", "top_right": "top-right corner", "top_center": "top center",
              "bottom_left": "bottom-left corner", "bottom_right": "bottom-right corner", "bottom_center": "bottom center"}


def _reader(ctx):
    if ctx.role not in ("owner", "member"):
        raise MarketingError("brand_kit_member_required", "Only Workspace members can read brand baselines.", 403)


def _product(session, ctx, product_id):
    product = session.scalar(select(ProductRecord).where(
        ProductRecord.workspace_id == ctx.workspace_id, ProductRecord.id == product_id))
    if product is None:
        raise MarketingError("brand_kit_product_not_found", "找不到这个产品。", 404)
    return product


def latest(session, ctx, product_id):
    return session.scalar(select(ProductBrandKitRecord).where(
        ProductBrandKitRecord.workspace_id == ctx.workspace_id, ProductBrandKitRecord.product_id == product_id)
        .order_by(ProductBrandKitRecord.version.desc()).limit(1))


def get_row(session, ctx, brand_kit_id):
    row = session.scalar(select(ProductBrandKitRecord).where(
        ProductBrandKitRecord.workspace_id == ctx.workspace_id, ProductBrandKitRecord.id == brand_kit_id))
    if row is None:
        raise MarketingError("brand_kit_not_found", "找不到这个品牌底线版本。", 404)
    return row


def view(session, ctx, row):
    logo = row.content_json.get("logo")
    try:
        available = bool(logo) and bool(logo_input(session, ctx, logo))
    except MarketingError:
        available = False
    return {"id": row.id, "product_id": row.product_id, "version": row.version, "content_hash": row.content_hash,
            "content": row.content_json, "created_by": row.created_by, "created_at": row.created_at.isoformat(),
            "logo_available": available}


def get(session, ctx, product_id):
    _reader(ctx)
    _product(session, ctx, product_id)
    row = latest(session, ctx, product_id)
    return {"product_id": product_id, "revision": row.version if row else 0,
            "current": view(session, ctx, row) if row else None}


def list_current(session, ctx):
    """Each product's latest version, for choosing a plan's brand baseline."""
    _reader(ctx)
    rows = session.scalars(select(ProductBrandKitRecord).where(ProductBrandKitRecord.workspace_id == ctx.workspace_id)
        .order_by(ProductBrandKitRecord.product_id, ProductBrandKitRecord.version.desc())).all()
    current = {}
    for row in rows:
        current.setdefault(row.product_id, row)
    names = dict(session.execute(select(ProductRecord.id, ProductRecord.name).where(
        ProductRecord.workspace_id == ctx.workspace_id, ProductRecord.id.in_(list(current)))).all()) if current else {}
    return {"items": [{"product_id": pid, "product_name": names.get(pid, ""), "current": view(session, ctx, row)}
                      for pid, row in sorted(current.items(), key=lambda pair: (names.get(pair[0], ""), pair[0]))]}


def _resolve_logo(session, ctx, product_id, logo):
    """Only the product's own released role=logo reference item may become its brand logo."""
    from app.library import service as library
    from app.products import reference_sets
    try:
        _, _, version, _ = reference_sets._load(session, ctx, product_id)
    except library.LibraryError:
        raise MarketingError("brand_logo_invalid", "请先在产品参考图中上传并标记 Logo。", fields=["body.logo.item_id"]) from None
    item = next((i for i in reference_sets._items(session, ctx, version.content_json or {}) if i["item_id"] == logo.item_id), None)
    if item is None or item["role"] != "logo":
        raise MarketingError("brand_logo_invalid", "请选择产品参考图中标记为 Logo 的图片。", fields=["body.logo.item_id"])
    if not item["available"]:
        raise MarketingError("brand_logo_unavailable", "Logo 图片尚未通过文件/权利检查或已撤回，请重新选择。", 409, fields=["body.logo.item_id"])
    obj = session.scalar(select(ReferenceObjectRecord).where(
        ReferenceObjectRecord.workspace_id == ctx.workspace_id, ReferenceObjectRecord.id == item["reference_object_id"]))
    return {"item_id": logo.item_id, "placement": logo.placement, "reference_version_id": item["reference_version_id"],
            "reference_object_id": obj.id, "content_sha256": obj.derivative_sha256}


def save(session, ctx, product_id, expected_revision, body):
    """Append the next version, or return the current one when nothing changed."""
    if ctx.role != "owner":
        raise MarketingError("owner_required", "Only the Workspace Owner can change a product's brand baseline.", 403)
    ensure_active(session, ctx)
    _product(session, ctx, product_id)
    current = latest(session, ctx, product_id)
    revision = current.version if current else 0
    if revision != expected_revision:
        raise MarketingError("stale_brand_kit_revision", "品牌底线已被更新，请刷新后重试。", 409)
    content = body.model_dump(mode="json")
    if body.logo:
        content["logo"] = _resolve_logo(session, ctx, product_id, body.logo)
    content = BrandKitContent.model_validate(content).model_dump(mode="json")
    if current and current.content_json == content:
        return current
    row = ProductBrandKitRecord(id=new_id("bk"), workspace_id=ctx.workspace_id, product_id=product_id,
        version=revision + 1, content_hash=digest(content), content_json=content, created_by=ctx.initiator_id)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        raise MarketingError("stale_brand_kit_revision", "品牌底线已被更新，请刷新后重试。", 409) from None
    append_event(session, event_type="marketing.brand_kit_saved", resource_type="product", resource_id=product_id,
                 details={"brand_kit_id": row.id, "version": row.version, "content_hash": row.content_hash})
    return row


def resolve_ref(session, ctx, ref, field="body.plan.brand_kit"):
    """The exact frozen version; a changed or foreign reference is refused, never re-pointed."""
    ref = BrandKitRef.model_validate(ref)
    row = session.scalar(select(ProductBrandKitRecord).where(
        ProductBrandKitRecord.workspace_id == ctx.workspace_id, ProductBrandKitRecord.id == ref.id))
    if row is None or row.product_id != ref.product_id or row.version != ref.version:
        raise MarketingError("brand_kit_not_found", "找不到方案引用的品牌底线版本。", 404, fields=[field])
    if row.content_hash != ref.content_hash or digest(row.content_json) != row.content_hash:
        raise MarketingError("brand_kit_changed", "方案引用的品牌底线版本与记录不一致，请重新选择。", 409, fields=[field])
    return row


def ref_of(row):
    return {"id": row.id, "product_id": row.product_id, "version": row.version, "content_hash": row.content_hash}


def default_ref(session, ctx, project):
    """The current kit of the product this test started from, offered for a first draft."""
    first = session.scalar(select(MarketingRevisionRecord).where(
        MarketingRevisionRecord.workspace_id == ctx.workspace_id, MarketingRevisionRecord.project_id == project.id,
        MarketingRevisionRecord.revision == 1))
    chosen = (first.content_json or {}).get("product_id") if first else None
    if not chosen:
        return None
    # A Library product is chosen by its entry id; its brand kit lives on the profiled product,
    # not on the compatibility anchor that reuses the entry id.
    candidates = session.scalars(select(ProductRecord.id).where(ProductRecord.workspace_id == ctx.workspace_id,
        (ProductRecord.id == chosen) | (ProductRecord.product_profile_entry_id == chosen))
        .order_by((ProductRecord.product_profile_entry_id == chosen).desc(), ProductRecord.id)).all()
    row = next((kit for kit in (latest(session, ctx, pid) for pid in candidates) if kit), None)
    return ref_of(row) if row else None


def logo_input(session, ctx, logo):
    """The frozen logo file for a provider or QC call, after re-checking its gates and hash."""
    from app.products.reference_sets import _available
    version = session.scalar(select(LibraryVersionRecord).where(
        LibraryVersionRecord.workspace_id == ctx.workspace_id, LibraryVersionRecord.id == logo["reference_version_id"]))
    obj = session.scalar(select(ReferenceObjectRecord).where(
        ReferenceObjectRecord.workspace_id == ctx.workspace_id, ReferenceObjectRecord.id == logo["reference_object_id"]))
    if (not version or not obj or (version.content_json or {}).get("object_id") != obj.id or not _available(session, ctx, version)
            or obj.derivative_sha256 != logo["content_sha256"] or not committed_key_in_workspace(obj.derivative_object_key or "", ctx.workspace_id)):
        raise MarketingError("brand_logo_unavailable", "品牌 Logo 已撤回、权利失效或文件已变化，请更新品牌底线后重新准备。", 409)
    blob = CommittedBlobRef(committed_key=obj.derivative_object_key, storage_version_id=obj.derivative_storage_version_id or "",
        size_bytes=int(obj.derivative_byte_size or 0), checksum_sha256_b64=base64.b64encode(bytes.fromhex(obj.derivative_sha256)).decode(),
        content_type_verified=obj.derivative_mime or "image/png")
    return {"reference_version_id": version.id, "reference_object_id": obj.id, "asset_version_id": version.id,
            "role": "brand_logo", "content_sha256": obj.derivative_sha256, "content_hash": version.content_hash,
            "blob": asdict(blob)}


def assert_logo_current(session, ctx, frozen):
    """A frozen logo is sent only while its file, rights and hash are unchanged."""
    if logo_input(session, ctx, frozen) != frozen:
        raise MarketingError("brand_logo_unavailable", "品牌 Logo 已撤回、权利失效或文件已变化，请更新品牌底线后重新准备。", 409)


def _percent(ratio):
    return f"{ratio * 100:g}%"


def brief_lines(content, *, logo_supplied, logo_in_source=False):
    """The English Brand section of an image brief. A revision sends no logo image: the original creative carries it."""
    rules = content["readability_rules"]
    fonts = content["fonts"]
    lines = ["Brand baseline (must not be broken by any style choice):"]
    if content.get("logo"):
        placement = PLACEMENTS[content["logo"]["placement"]]
        lines.append(f"- Logo: keep the brand logo area in the {placement} exactly as in the original creative, inside the safe area; "
                     "never redraw, distort, recolor or invent a logo." if logo_in_source else
                     f"- Logo: place the brand logo from the last reference image in the {placement}, inside the safe area; "
                     "reproduce it exactly and never redraw, distort, recolor or invent a logo."
                     if logo_supplied else f"- Logo: keep a clean {placement} area for the brand logo; never draw or invent a logo.")
    lines.append(f"- Palette: use only {', '.join(content['palette'])} for backgrounds, graphics and text; keep the product's true colors.")
    lines.append(f"- Typography: headline in a {fonts['primary']}-style typeface"
                 + (f", body text in a {fonts['secondary']}-style typeface" if fonts.get("secondary") else "") + ".")
    lines.append(f"- Readability: text at least {_percent(rules['min_text_height_ratio'])} of image height, contrast at least "
                 f"{rules['min_contrast_ratio']:g}:1, all text and the logo inside a {_percent(rules['safe_area_ratio'])} safe margin; "
                 "no cropped, overlapping or garbled text.")
    if content.get("forbidden"):
        lines.append("- Never show or claim: " + "; ".join(content["forbidden"]) + ".")
    return lines


def qc_summary(row):
    """The frozen baseline the vision check compares against."""
    content = row.content_json
    return {"ref": ref_of(row), "logo": {"required": bool(content.get("logo")),
                **({"placement": content["logo"]["placement"]} if content.get("logo") else {})},
            "palette": content["palette"], "fonts": content["fonts"],
            "readability_rules": content["readability_rules"], "forbidden": content.get("forbidden", [])}
