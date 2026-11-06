"""Product/model reference settings, stored as immutable Library bindings.

Every edit publishes a ProductVersion and materializes its ReferenceBindings.
There is no second product identity or mutable file copy. Older versions (and
their removed bindings) remain available to frozen runs and Library history.
Uploads must first finish Upload Intent/Commit, Worker gate and promotion;
this module only accepts those released, published reference identities.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    LibraryEntryRecord, LibraryVersionRecord, ProductRecord, ReferenceObjectRecord, new_id, utcnow,
)
from app.library import service as library
from app.library.contracts import check_rights_eligible
from app.platform import audit

MAX_BYTES = 10 * 1024 * 1024
MAX_ITEMS = 8
ROLES = {"primary", "detail", "logo", "certification_mark"}
STATUSES = {"verified_physical", "unverified"}
METADATA = "product_reference_item"


def _member(ctx):
    if ctx.role not in {"owner", "member"}:
        raise library.LibraryForbidden("Only Workspace members may manage product references.")


def _load(session, ctx, product_id):
    product = session.scalar(select(ProductRecord).where(
        ProductRecord.id == product_id, ProductRecord.workspace_id == ctx.workspace_id,
    ))
    if product is None:
        raise library.LibraryNotFound("Product not found.", code="product_not_found")
    if not product.product_profile_entry_id:
        raise library.LibraryConflict("Publish the product card in Creative Library first.",
                                      {"code": "product_reference_profile_required"})
    entry = library._entry(session, ctx, product.product_profile_entry_id)
    version = library._effective_version(session, entry)
    if entry.lifecycle != "active" or version is None or version.eligibility == "blocked":
        raise library.LibraryConflict("Publish an active product card first.",
                                      {"code": "product_reference_profile_required"})
    draft = library._draft(session, ctx, entry.id, for_update=True)
    return product, entry, version, draft


def _models(content):
    return [str(item["label"]) for item in content.get("model_variants", [])
            if isinstance(item, dict) and item.get("label")]


def _items(session, ctx, content):
    """Legacy bindings have stable derived ids and remain unverified."""
    result = []
    models = _models(content)
    bindings = content.get("reference_bindings") or []
    physical = [(index, binding) for index, binding in enumerate(bindings)
                if binding.get("purpose") not in {"logo", "certification_mark"}]
    primary_index = min(physical, key=lambda pair: (int(pair[1].get("priority") or 0), pair[0]))[0] if physical else None
    for index, binding in enumerate(bindings):
        version_id = binding.get("reference_version_id")
        version = session.scalar(select(LibraryVersionRecord).where(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.id == version_id,
        ))
        metadata = dict((binding.get("conditions") or {}).get(METADATA) or {})
        stable_id = "legacy_" + hashlib.sha256(
            f"{version_id}:{binding.get('purpose')}:{binding.get('viewpoint')}".encode()
        ).hexdigest()[:24]
        result.append({
            "item_id": metadata.get("item_id", stable_id),
            "reference_object_id": (version.content_json or {}).get("object_id", "") if version else "",
            "reference_version_id": version_id,
            "model_variant": metadata.get("model_variant", models[0] if len(models) == 1 else ""),
            "role": metadata.get("role", binding["purpose"] if binding.get("purpose") in {"logo", "certification_mark"}
                                 else "primary" if index == primary_index else "detail"),
            "identity_status": metadata.get("identity_status", "unverified"),
            "note": binding.get("note") or "",
            "added_by": metadata.get("added_by", version.published_by if version else None),
            "added_at": metadata.get("added_at", version.published_at.isoformat() if version else ""),
            "available": _available(session, ctx, version) if version else False,
        })
    return result


def _available(session, ctx, version):
    reference_entry = library._entry(session, ctx, version.entry_id)
    obj = session.scalar(select(ReferenceObjectRecord).where(
        ReferenceObjectRecord.id == (version.content_json or {}).get("object_id"),
        ReferenceObjectRecord.workspace_id == ctx.workspace_id,
    ))
    return bool(version.entry_type == "reference_asset" and version.state == "effective"
                and version.eligibility != "blocked" and reference_entry.lifecycle == "active"
                and check_rights_eligible((version.content_json or {}).get("rights") or {}) is None
                and obj and obj.state == "released" and obj.derivative_sha256
                and obj.derivative_object_key)


def _view(session, ctx, product, entry, version, draft):
    return {"product_id": product.id, "entry_id": entry.id, "version_id": version.id,
            "revision": draft.revision, "model_variants": _models(version.content_json or {}),
            "has_unpublished_changes": draft.content_json != version.content_json,
            "items": _items(session, ctx, version.content_json or {})}


def published(session, ctx, product):
    """The effective ProductVersion's items for frozen consumers; never the unpublished draft."""
    if not product.product_profile_entry_id:
        return None
    entry = session.scalar(select(LibraryEntryRecord).where(
        LibraryEntryRecord.workspace_id == ctx.workspace_id, LibraryEntryRecord.id == product.product_profile_entry_id,
    ))
    version = library._effective_version(session, entry) if entry is not None and entry.lifecycle == "active" else None
    if version is None or version.eligibility == "blocked":
        return None
    content = version.content_json or {}
    return {"version_id": version.id, "model_variants": _models(content), "items": _items(session, ctx, content)}


def get(ctx, product_id):
    _member(ctx)
    with tenant_session(ctx) as session:
        return _view(session, ctx, *_load(session, ctx, product_id))


def mutate(ctx, product_id, *, action, expected_revision, payload, item_id=None,
           idempotency_key):
    _member(ctx)
    fingerprint = library.compute_fingerprint({"product_id": product_id, "action": action,
        "item_id": item_id, "revision": expected_revision, "payload": payload})

    def body(session, key_hash):
        product, entry, version, draft = _load(session, ctx, product_id)
        if draft.revision != expected_revision:
            raise library.RevisionConflict("References changed. Refresh and try again.",
                                          {"current_revision": draft.revision})
        if draft.content_json != version.content_json:
            raise library.LibraryConflict("Finish or discard the product's Creative Library draft first.",
                                          {"code": "product_reference_draft_conflict"})
        content = deepcopy(version.content_json or {})
        bindings = content.get("reference_bindings") or []
        items = _items(session, ctx, content)
        for binding, item in zip(bindings, items, strict=True):
            binding["conditions"] = dict(binding.get("conditions") or {})
            binding["conditions"][METADATA] = {
                key: item[key] for key in ("item_id", "model_variant", "role", "identity_status", "added_by", "added_at")
            }

        if action == "add":
            model = str(payload.get("model_variant") or "")
            if _models(content) and not model and payload["role"] != "logo":
                raise library.LibraryConflict("Choose the exact product model for this reference. Only logos may be shared across models.",
                                              {"code": "product_reference_model_required"})
            if model and model not in _models(content):
                raise library.LibraryConflict("Choose a model listed on the product card.",
                                              {"code": "product_reference_model_invalid"})
            if sum(item["model_variant"] == model for item in items) >= MAX_ITEMS:
                raise library.LibraryConflict("Each product/model can have at most eight reference images.",
                                              {"code": "product_reference_limit"})
            reference_id = payload.get("reference_version_id")
            if not reference_id:
                obj = session.scalar(select(ReferenceObjectRecord).where(
                    ReferenceObjectRecord.id == payload.get("reference_object_id"),
                    ReferenceObjectRecord.workspace_id == ctx.workspace_id,
                ))
                if obj is None or not obj.promoted_entry_id:
                    raise library.LibraryNotFound("Published reference object not found.", code="reference_object_not_found")
                reference_id = library._entry(session, ctx, obj.promoted_entry_id).current_version_id
            reference = session.scalar(select(LibraryVersionRecord).where(
                LibraryVersionRecord.id == reference_id, LibraryVersionRecord.workspace_id == ctx.workspace_id,
            ))
            if reference is None:
                raise library.LibraryNotFound("Reference not found.", code="product_reference_not_found")
            if not _available(session, ctx, reference):
                raise library.LibraryConflict("Reference has not passed file/rights checks or was withdrawn.",
                                              {"code": "product_reference_unavailable"})
            obj = session.get(ReferenceObjectRecord, reference.content_json["object_id"])
            if obj.byte_size > MAX_BYTES:
                raise library.LibraryConflict("Choose an image of 10 MB or smaller.",
                                              {"code": "product_reference_too_large"})
            if any(item["reference_object_id"] == obj.id and item["model_variant"] == model for item in items):
                raise library.LibraryConflict("This image is already in this model's reference set.",
                                              {"code": "product_reference_duplicate"})
            metadata = {"item_id": new_id("pritem"), "model_variant": model,
                "role": payload["role"], "identity_status": payload["identity_status"],
                "added_by": ctx.initiator_id, "added_at": utcnow().isoformat()}
            target = {"reference_version_id": reference.id, "purpose": "product_reference",
                "viewpoint": metadata["item_id"], "priority": 10, "note": payload.get("note", ""),
                "conditions": {METADATA: metadata}}
            bindings.append(target)
        else:
            target = next((binding for binding in bindings
                           if binding["conditions"][METADATA]["item_id"] == item_id), None)
            if target is None:
                raise library.LibraryNotFound("Product reference item not found.", code="product_reference_not_found")
            if action == "delete":
                bindings.remove(target)  # Previous immutable ProductVersions retain this binding.
            else:
                metadata = target["conditions"][METADATA]
                if (_models(content) and not metadata["model_variant"]
                        and payload.get("role", metadata["role"]) != "logo"
                        and ("role" in payload or payload.get("identity_status") == "verified_physical")):
                    raise library.LibraryConflict("Remove and re-add this image under its exact product model before verifying it.",
                                                  {"code": "product_reference_model_required"})
                metadata.update({key: value for key, value in payload.items() if key != "note"})
                if "note" in payload:
                    target["note"] = payload["note"]

        if action == "add" or (action == "update" and "role" in payload):
            meta = target["conditions"][METADATA]
            target["priority"] = 0 if meta["role"] == "primary" else 10
            target["purpose"] = meta["role"] if meta["role"] in {"logo", "certification_mark"} else "product_reference"
            if meta["role"] == "primary":
                for binding in bindings:
                    other = binding["conditions"][METADATA]
                    if binding is not target and other["model_variant"] == meta["model_variant"] and other["role"] == "primary":
                        other["role"] = "detail"
                        binding["priority"] = 10
                        binding["purpose"] = "product_reference"
        # Unchanged Library purposes, viewpoints and priorities retain their
        # original semantics; a note/verification edit only changes that field.
        content["reference_bindings"] = bindings
        if content == version.content_json:
            return _view(session, ctx, product, entry, version, draft)
        report, resolved = library._build_report(session, ctx, entry, content)
        if not report.ok or report.warnings:
            raise library.LibraryValidationError("Review product validation in Creative Library first.", report.as_dict())
        library._publish_effective_version(session, ctx, entry, draft, content, report, resolved, set(),
            expected_revision, extra_audit_detail={"product_reference_action": action})
        # The publish helper expires the Draft during its revision CAS. Set
        # its new base content afterwards, as save_draft does, so a subsequent
        # settings edit sees a clean draft rather than the previous version.
        draft.content_json = deepcopy(content)
        audit.append_event(session, event_type=f"product_reference.{action}", resource_type="product",
            resource_id=product_id, details={"item_id": target["conditions"][METADATA]["item_id"],
                "version_id": entry.current_version_id, "idempotency_key_hash": key_hash})
        current = session.get(LibraryVersionRecord, entry.current_version_id)
        return _view(session, ctx, product, entry, current, draft)

    return library._run_with_idempotency(ctx, idempotency_key, f"product_reference_{action}", fingerprint, body)
