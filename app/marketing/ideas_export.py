"""Ads-first verification, ad status and download (#198; ADR 0018 §2-§3, design doc §5.3 and §6).

A draft may be made from facts and photos nobody checked; only a checked ad may leave. An ad of ads-first
ideas is ``exportable`` when all three hold, and nothing else stops its image:

1. every fact it cites is checked by the user and still reads as it did when the image was made;
2. the product photos it used are checked and still usable -- or, made without one, the user confirmed
   that the product in the image looks right;
3. its automatic check finished, every finding that needs attention was handled by the user, and the
   check was made against the facts as they are now.

``needs_redo`` is an ad no checking can rescue: a fact it cites was changed or removed, its content is no
longer what the ideas say under that combination, a photo it used is no longer usable, or the image check
of its generation stopped the image. Everything else that is not exportable is a ``draft``.

The status is derived, never stored (design doc §7): from the image's recipe (the understanding as it
was, the cited facts, the photos sent), the project's facts as they are, the image's newest check, and the
append-only verification records (``MarketingIdeasExportRecord``). Checking never rewrites an image, a
recipe or a check; it only adds what the user confirmed.

Only what the user checked becomes confirmed content: each verification publishes the checked facts and
photos as the product's creative-library version, while the library Draft keeps the AI's whole proposal.

Photos (#199): an ad may be drafted with a photo the product's reading found that nobody checked yet.
The photo is its content hash. Checking it -- this is the product's real photo and it may be used in ads
-- makes every ad made with those bytes count as made with a checked photo, with no new image; rejecting
it makes them ``needs_redo``. Each decision is a verification record's line; the newest one counts.
"""
from __future__ import annotations

from datetime import date, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.adapters.persistence.models import LibraryDraftRecord, LibraryEntryRecord, ProductRecord, new_id, utcnow
from app.generation.models import RunItemRecord
from app.library.contracts import PRODUCT_SOURCE_URL_MAX
from app.platform.audit import append_event
from . import ideas, ideas_checks, service
from .contracts import Understanding
from .models import MarketingIdeasExportRecord as Record, MarketingProjectRecord
from .product_references import references_current
from .service import MarketingError, digest

#: Why an ad needs making again; any other reason leaves it a draft.
REDO_REASONS = frozenset({"ideas_changed", "facts_changed", "facts_removed", "photo_unavailable", "image_blocked"})
#: Attestations of a frozen product photo that mean a person checked it against the real product.
VERIFIED_PHOTOS = ("verified_physical_product", "user_confirmed_real_product")
SOURCE_LIMIT = 16000


def _text(fact) -> tuple:
    return fact.get("statement"), fact.get("source_quote")


def fact_hash(fact) -> str:
    """What a checked fact said when the user checked it."""
    return digest({"statement": fact["statement"], "source_quote": fact["source_quote"]})


def _references(recipe) -> list:
    return ((recipe.canonical_json or {}).get("selection") or {}).get("product_references") or []


def confirmations(session, ctx, row):
    """What the user confirmed about the ads of these ideas, over every verification: the ads whose
    appearance was confirmed, and per (ad, check) the findings accepted as they are."""
    appearance, findings = set(), {}
    for record in session.scalars(select(Record).where(Record.workspace_id == ctx.workspace_id, Record.project_id == row.id,
                                                       Record.kind == "verification")).all():
        appearance.update(record.content_json.get("appearance", []))
        for item in record.content_json.get("findings", []):
            findings.setdefault((item["asset_version_id"], item["check_id"]), set()).update(item["finding_ids"])
    return appearance, findings


def _reason(name: str, *, fact_ids=(), finding_ids=()) -> dict:
    """One ``AdReason``: why an ad is not exportable (a status reason, not an error the API raises)."""
    return dict(code=name, fact_ids=list(fact_ids), finding_ids=list(finding_ids))


def _settled_by_appearance(finding) -> bool:
    """The check could not judge the product's look from the image alone; the user's own confirmation does."""
    return finding["check_id"] == "product_fidelity" and finding["status"] == "unknown"


def _photo_rejected(session, ctx, references, sets) -> bool:
    """Whether a photo frozen from the product's own photo set is no longer a checked photo there.

    Such a photo counts as checked only while the product's published set still lists it as a verified
    photo of the real product. Marking it unverified, or taking it off the product, rejects it (design
    doc §5.3) although the file and its rights are untouched. ``sets`` caches each product's set."""
    from app.products.reference_sets import published
    from .product_references import SET_POLICY
    for reference in references:
        if reference.get("selection_policy") != SET_POLICY:
            continue
        product_id = reference.get("product_id")
        if product_id not in sets:
            product = session.scalar(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
                                                                 ProductRecord.id == product_id))
            current = published(session, ctx, product) if product is not None else None
            sets[product_id] = {item["reference_version_id"] for item in (current or {}).get("items", [])
                                if item.get("identity_status") == "verified_physical"}
        if reference.get("reference_version_id") not in sets[product_id]:
            return True
    return False


def _stamp(moment) -> str:
    """A record's time as the same text whether it was just made or read back (naive and aware alike: UTC)."""
    if moment is None:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.isoformat(timespec="seconds")


def photo_decisions(session, ctx, row, pending=()) -> dict:
    """The user's newest decision on each photo of this product (#199), by photo id, over every verification.

    A photo is the product's, not one project's: a decision made in any ads-first project of the same
    product counts for all of them, the newest one winning. Each value is the record's line --
    ``decision`` (``verified`` / ``rejected``), the reading's image, and for a checked photo its
    creative-library reference -- with the record's ``project_id``, ``actor_id`` and ``decided_at``.
    ``pending``: the lines a verification of ``row`` is about to record; they are the newest."""
    latest = {}
    for record in session.scalars(select(Record).join(MarketingProjectRecord,
            (MarketingProjectRecord.id == Record.project_id) & (MarketingProjectRecord.workspace_id == ctx.workspace_id))
            .where(Record.workspace_id == ctx.workspace_id, Record.kind == "verification",
                   MarketingProjectRecord.product_id == row.product_id, MarketingProjectRecord.flow == ideas.FLOW)
            .order_by(Record.created_at, Record.project_revision, Record.id)).all():
        for item in record.content_json.get("photos", []):
            latest[item["photo_id"]] = {**item, "project_id": record.project_id, "actor_id": record.actor_id,
                                        "decided_at": _stamp(record.created_at)}
    for item in pending:
        latest[item["photo_id"]] = {**item, "project_id": row.id}
    return latest


def _reference_usable(session, ctx, line) -> bool:
    """Whether the creative-library reference a checked photo became can still be used: not withdrawn,
    archived or blocked, its rights still valid, its file still released (``product_references._released``)."""
    from .product_references import _released
    if not line.get("reference_version_id"):
        return False
    try:
        _released(session, ctx, line["reference_version_id"])
    except MarketingError:
        return False
    return True


def photo_states(session, ctx, row, found=None, verified=None, decisions=None) -> dict:
    """Each photo's state for these ideas, by photo id: ``verified``, ``rejected``, ``unavailable`` or ``unverified``.

    The user's newest decision on the product's photo counts first. A checked photo whose creative-library
    reference can no longer be used (withdrawn, archived, its rights expired) is ``unavailable``: it is not
    confirmed content any more, and nothing made with it may leave. Without a decision, a photo whose
    content the product's photo set lists as a verified photo of the real product is ``verified``: the same
    bytes checked once are checked wherever they were sent (#199). ``found`` / ``verified`` / ``decisions``
    are the reading's photos (``product_references.candidates``), the product's verified photos
    (``from_product_set``) and ``photo_decisions``, when the caller has them."""
    from .product_references import candidates, from_product_set
    found = candidates(session, ctx, row) if found is None else found
    verified = from_product_set(session, ctx, row) if verified is None else verified
    decisions = photo_decisions(session, ctx, row) if decisions is None else decisions
    library = {ref["content_sha256"] for ref in verified}
    ids = [photo["photo_id"] for photo in found] + sorted(library) + list(decisions)

    def state(pid):
        line = decisions.get(pid)
        if line is None:
            return "verified" if pid in library else "unverified"
        if line["decision"] == "verified" and not _reference_usable(session, ctx, line):
            return "unavailable"
        return line["decision"]
    return {pid: state(pid) for pid in dict.fromkeys(ids)}


#: Photo states nothing new may be made with, and that make every ad made with the photo need redoing.
PHOTO_GONE = ("rejected", "unavailable")


def photos_gone(session, ctx, row, references) -> bool:
    """Whether any of these frozen photos was rejected, or checked and its library reference is gone since."""
    decided = photo_decisions(session, ctx, row)
    for reference in references:
        line = decided.get(reference.get("content_sha256"))
        if line is not None and (line["decision"] == "rejected" or not _reference_usable(session, ctx, line)):
            return True
    return False


def photo_list(session, ctx, row, ads) -> list[dict]:
    """The ``IdeasPhoto`` list: the photos the product's reading found, each with its state and its ads."""
    from .product_references import candidates, from_product_set
    found, verified = candidates(session, ctx, row), from_product_set(session, ctx, row)
    decisions = photo_decisions(session, ctx, row)
    states = photo_states(session, ctx, row, found, verified, decisions)
    library = {ref["content_sha256"]: ref["reference_version_id"] for ref in verified}
    used = {}
    for version, recipe in ads:
        for reference in _references(recipe):
            used.setdefault(reference.get("content_sha256"), []).append(version.id)
    result = []
    for photo in found:
        pid, state = photo["photo_id"], states[photo["photo_id"]]
        result.append({"photo_id": pid, "status": state, "role": photo["role"], "source": photo["source"],
                       "source_url": photo["source_url"], "intake_id": photo["intake_id"], "image_id": photo["image_id"],
                       "width": photo["width"], "height": photo["height"],
                       "reference_version_id": (library.get(pid) or decisions.get(pid, {}).get("reference_version_id"))
                       if state == "verified" else None,
                       "asset_version_ids": used.get(pid, [])})
    return result


def statuses(session, ctx, row, ads=None, calls=None, states=None) -> dict:
    """Each ad's status, why it is not exportable, the facts it stands on and whether its look is confirmed."""
    ads = ideas.project_ads(session, ctx, row) if ads is None else ads
    calls = ideas_checks.latest_by_asset(session, ctx, row.id) if calls is None else calls
    states = photo_states(session, ctx, row) if states is None else states
    appearance, accepted = confirmations(session, ctx, row)
    item_ids = [version.producing_run_item_id for version, _ in ads if version.producing_run_item_id]
    items = dict(session.execute(select(RunItemRecord.id, RunItemRecord.state).where(
        RunItemRecord.workspace_id == ctx.workspace_id, RunItemRecord.id.in_(item_ids))).all()) if item_ids else {}
    facts = {fact["id"]: fact for fact in (row.understanding_json or {}).get("facts", [])}
    current, photo_sets = {}, {}

    def now(combination_id):
        """The content the ideas hold for this combination now, and every fact id its claim cites."""
        if combination_id not in current:
            try:
                current[combination_id] = (ideas.content_of(ideas.ideas_intent(row, combination_id)),
                                           ideas.citations(row, combination_id))
            except LookupError:
                current[combination_id] = None
        return current[combination_id]

    result = {}
    for version, recipe in ads:
        intent = ideas.intent_of(recipe)
        reasons = []
        held = now((intent.get("combination") or {}).get("id", ""))
        # Redrafted ideas reuse combination ids: an image whose content is not the ideas' is another ad's (#212).
        same = held is not None and held[0] == ideas.content_of(intent)
        if not same:
            reasons.append(_reason("ideas_changed"))
        snapshot = {fact["id"]: fact for fact in (intent.get("understanding") or {}).get("facts", [])}
        cited = list(intent.get("cited_fact_ids") or [])
        # What the claim cites now but this image was not made on: a removed fact the claim still names (an
        # image made after the removal froze one citation fewer), an id reused for another fact, an
        # inference pointed at another fact, ideas redrafted in the same words on other facts.
        unseen = sorted(fid for fid in held[1] if fid not in cited) if same else []
        removed = [fid for fid in cited if fid not in facts] + [fid for fid in unseen if fid not in facts]
        changed = [fid for fid in cited if fid in facts and (fid not in snapshot or _text(facts[fid]) != _text(snapshot[fid]))]
        changed += [fid for fid in unseen if fid in facts]
        unverified = [fid for fid in cited if fid in facts and fid not in changed and facts[fid].get("confirmation") != "confirmed"]
        for name, ids in (("facts_changed", changed), ("facts_removed", removed), ("facts_unverified", unverified)):
            if ids:
                reasons.append(_reason(name, fact_ids=ids))
        references = _references(recipe)
        if references:
            # A photo the user rejected, or one no longer usable as it was sent or as it was checked, cannot be
            # rescued (#199).
            if (not references_current(session, ctx, references) or _photo_rejected(session, ctx, references, photo_sets)
                    or any(states.get(reference.get("content_sha256")) in PHOTO_GONE for reference in references)):
                reasons.append(_reason("photo_unavailable"))
            elif any(reference.get("attestation") not in VERIFIED_PHOTOS
                     and states.get(reference.get("content_sha256")) != "verified" for reference in references):
                reasons.append(_reason("photo_unverified"))
        elif version.id not in appearance:
            reasons.append(_reason("appearance_unconfirmed"))
        state = items.get(version.producing_run_item_id)
        if state in ("failed", "cancelled"):  # the image exists, but its generation's own check stopped it
            reasons.append(_reason("image_blocked"))
        elif state not in (None, "succeeded", "reused"):
            reasons.append(_reason("image_check_in_progress"))
        call = calls.get(version.id)
        if call is None:
            reasons.append(_reason("check_missing"))
        elif call.state != "succeeded":
            reasons.append(_reason({"queued": "check_in_progress", "running": "check_in_progress",
                                    "uncertain": "check_uncertain"}.get(call.state, "check_failed")))
        else:
            taken = accepted.get((version.id, call.id), set())
            confirmed_look = not references and version.id in appearance
            pending = [finding["check_id"] for finding in ideas_checks.projection(call)["findings"]
                       if finding["check_id"] not in taken and not (confirmed_look and _settled_by_appearance(finding))]
            if pending:
                reasons.append(_reason("findings_open", finding_ids=pending))
            checked = {fact["id"]: fact for fact in (call.input_json.get("understanding") or {}).get("facts", [])}
            if any(fid not in facts or fid not in checked or _text(facts[fid]) != _text(checked[fid])
                   for fid in call.input_json.get("evidence_fact_ids") or []):
                reasons.append(_reason("check_outdated"))
        status = "exportable" if not reasons else "needs_redo" if any(r["code"] in REDO_REASONS for r in reasons) else "draft"
        result[version.id] = {"status": status, "cited_fact_ids": cited, "appearance_confirmed": version.id in appearance,
                              "reasons": reasons}
    return result


def ad_status(session, ctx, row, asset_version_id: str) -> dict:
    """One ad's status (``statuses``); ``ad_not_found`` when the image is not an ad of these ideas."""
    result = statuses(session, ctx, row)
    if asset_version_id not in result:
        raise MarketingError("ad_not_found", "这张广告不属于这些广告想法。", 404)
    return result[asset_version_id]


def _require_understanding(row) -> None:
    if row.state not in ideas.DRAFTABLE or not row.understanding_json:
        raise MarketingError("understanding_required", "请先生成或完善产品理解。")


def _actor(ctx):
    return ctx.initiator_id or ctx.executor


def edit_understanding(session, ctx, row, revision, understanding: Understanding, source_addition: str | None = None):
    """Change what the AI read about the product: facts, guesses, unknowns; the ideas stay as they are.

    A fact is checked only by ``verify``: the ``confirmation`` sent here is ignored. A fact that reads as
    before keeps its checking; a changed or new fact is unchecked. Ads that cite a changed or removed
    fact become ``needs_redo`` on their own (``statuses`` compares each image's recipe with the facts),
    and ads that cite neither are untouched. A quote must be found in the product's source material;
    ``source_addition`` adds material the user supplies for a quote, never replacing what was read.
    A checked fact that was changed or removed leaves the product's published version too."""
    _require_understanding(row)
    source = row.source_text
    if source_addition and source_addition.strip():
        source = f"{row.source_text}\n\n{source_addition.strip()}"
        if len(source) > SOURCE_LIMIT:
            raise MarketingError("source_invalid", "产品资料加起来不能超过 16000 字。", params={"limit": SOURCE_LIMIT})
    service.validate_understanding(SimpleNamespace(source_text=source), understanding)
    stored = {fact["id"]: fact for fact in row.understanding_json.get("facts", [])}
    facts = []
    for fact in understanding.facts:
        before = stored.get(fact.id)
        kept = bool(before) and before.get("confirmation") == "confirmed" and _text(before) == (fact.statement, fact.source_quote)
        facts.append(fact.model_copy(update={"confirmation": "confirmed" if kept else "unconfirmed"}))
    document = understanding.model_copy(update={"facts": facts}).model_dump(mode="json")
    if document == row.understanding_json and source == row.source_text:
        if row.revision != revision:
            raise MarketingError("stale_marketing_revision", "内容已更新，请刷新后重试。", 409)
        return row
    after = {fact["id"]: fact for fact in document["facts"]}
    details = {"changed": sorted(fid for fid in after if fid in stored and _text(after[fid]) != _text(stored[fid])),
               "removed": sorted(fid for fid in stored if fid not in after),
               "added": sorted(fid for fid in after if fid not in stored), "source_added": source != row.source_text}
    service.advance(session, ctx, row, revision, "understanding_edited", understanding_json=document,
                    **({"source_text": source} if source != row.source_text else {}))
    append_event(session, event_type="marketing.ideas_facts_edited", resource_type="marketing_project", resource_id=row.id,
                 details={"revision": row.revision, **details, "library_version_id": publish_verified(session, ctx, row)})
    session.flush()
    return row


def _photo_binding(photo, primary: bool) -> dict:
    """A checked photo as the product version's reference binding, the way the product's photo set reads it
    (``reference_sets``): a verified photo of the real product, of the one model the version has."""
    from app.products.reference_sets import METADATA
    item_id = f"pritem_{photo['photo_id'][:24]}"
    return {"reference_version_id": photo["reference_version_id"], "purpose": "product_reference", "viewpoint": item_id,
            "priority": 0 if primary else 10, "note": f"product_intake:{photo['intake_id']}:{photo['image_id']}",
            "conditions": {METADATA: {"item_id": item_id, "model_variant": "", "role": "primary" if primary else "detail",
                                      "identity_status": "verified_physical", "added_by": photo.get("actor_id"),
                                      "added_at": photo.get("decided_at") or ""}}}


def verified_content(name: str, source_url, statements: list, markets: list, photos: list = ()) -> dict:
    """The product version a verification publishes: nothing the user did not check or choose.

    The checked facts are its description and selling points. A product version must name an audience
    (creative-library contract §5.1), and the only audience the user has chosen is the market on the
    start page; the AI's guess about the buyers stays a proposal in the library Draft. The name is the
    product's own, and ``source_url`` the link the user pasted. ``photos`` (#199): the checked photos, the
    main one first, as verified reference bindings."""
    content = {"name": name, "description": "\n".join(statements) or name, "selling_points": statements,
               "audience": ", ".join(markets)}
    if (isinstance(source_url, str) and source_url.strip().lower().startswith(("http://", "https://"))
            and len(source_url) <= PRODUCT_SOURCE_URL_MAX):
        content["source_url"] = source_url.strip()
    if photos:
        content["reference_bindings"] = [_photo_binding(photo, index == 0) for index, photo in enumerate(photos)]
    return content


def _checked_photos(session, ctx, row, pending=()) -> list:
    """The product's checked photos that are still usable, in the page's order: a decision line each.

    A photo whose newest decision is a rejection is left out, and so is one whose creative-library
    reference can no longer be used (withdrawn, archived, its rights expired): a version naming it would
    not pass the library's validation, and it is no longer confirmed content."""
    decided = photo_decisions(session, ctx, row, pending)
    return sorted((item for item in decided.values() if item["decision"] == "verified" and _reference_usable(session, ctx, item)),
                  key=lambda item: (item.get("rank") or 10_000, item["photo_id"]))


def publish_verified(session, ctx, row, *, excluding: str | None = None, pending=()) -> str | None:
    """Publish what the user has checked as the product's creative-library version; its id, or ``None``.

    The version holds the facts checked in every ads-first project of the product, oldest project first,
    so one project's edit never drops what another has checked, and the product's checked photos (the
    newest decision on each photo, from whichever project). ``excluding`` leaves one project's facts out:
    its understanding is about to be drafted again and will come back unchecked (photos stay: they do not
    come from the understanding). ``pending``: this project's photo decisions a verification is about to
    record (``photo_decisions``).

    Only checked content goes in (``verified_content``): the library Draft keeps the AI's whole proposal
    and is not touched. A rejected photo is left out, so a photo checked before and rejected now (in any
    project of the product) leaves the version; so is a checked photo whose creative-library reference can
    no longer be used, which would otherwise block every later publish. Nothing is published while nothing is checked, for a product without a library entry, or for
    a product the user ever published themself from its card -- that is already the user's confirmed
    content (or their decision to withdraw it) and is left alone. Checking more, or changing a checked
    fact, replaces the version an earlier verification published."""
    from app.library import service as library
    product = session.scalar(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
                                                         ProductRecord.id == row.product_id))
    entry = session.scalar(select(LibraryEntryRecord).where(LibraryEntryRecord.workspace_id == ctx.workspace_id,
        LibraryEntryRecord.id == product.product_profile_entry_id)) if product and product.product_profile_entry_id else None
    if entry is None or entry.lifecycle != "active":
        return None
    statements, markets = [], []
    # Photos are the product's: the newest decision on each, whichever project made it (``photo_decisions``).
    photos = _checked_photos(session, ctx, row, pending)
    photo_projects = {photo["project_id"] for photo in photos}
    for project in session.scalars(select(MarketingProjectRecord).where(
            MarketingProjectRecord.workspace_id == ctx.workspace_id, MarketingProjectRecord.product_id == row.product_id,
            MarketingProjectRecord.flow == ideas.FLOW).order_by(MarketingProjectRecord.created_at, MarketingProjectRecord.id)).all():
        checked = [fact["statement"] for fact in (project.understanding_json or {}).get("facts", [])
                   if fact.get("confirmation") == "confirmed"] if project.id != excluding else []
        statements += [statement for statement in dict.fromkeys(checked) if statement not in statements]
        # The audience is the market of each project that has something checked: a project's market is the
        # user's choice, but it joins the published version only together with that project's checked content.
        if (checked or project.id in photo_projects) and project.market and project.market not in markets:
            markets.append(project.market)
    markets = markets or ([row.market] if row.market else [])
    if not markets or (not statements and not photos and not entry.current_version_id):
        return None
    # One main photo: the first one the reading called the main photo, else the first checked.
    main = next((photo for photo in photos if photo.get("role") == "main"), photos[0] if photos else None)
    photos = [main, *(photo for photo in photos if photo is not main)] if main else []
    draft = session.scalar(select(LibraryDraftRecord).where(LibraryDraftRecord.workspace_id == ctx.workspace_id,
                                                            LibraryDraftRecord.entry_id == entry.id))
    content = verified_content(entry.display_name, ((draft.content_json if draft else None) or {}).get("source_url"),
                               statements, markets, photos)
    try:
        version = library.publish_content_in_transaction(
            session, ctx, entry.id, content, origin_command=library.COMMAND_IDEAS_VERIFICATION,
            replaces=frozenset({library.COMMAND_IDEAS_VERIFICATION}))
    except library.LibraryError as exc:
        raise MarketingError("verified_product_unpublishable", "核对过的内容不能发布为产品版本，请检查产品资料后重试。", 409,
                             params={"reason": getattr(exc, "code", None) or "library_validation_failed"}) from exc
    return version.id if version is not None else None


def _ads(session, ctx, row, asset_version_ids) -> dict:
    ads = {version.id: (version, recipe) for version, recipe in ideas.project_ads(session, ctx, row)}
    if any(asset_id not in ads for asset_id in asset_version_ids):
        raise MarketingError("ad_not_found", "这张广告不属于这些广告想法。", 404)
    return ads


def _photo_decisions_to_record(session, ctx, row, photo_ids, rejected_photo_ids) -> list:
    """The photo lines of a verification (#199), each naming one photo the product's reading found.

    A checked photo becomes a creative-library reference here, in this command: the checking user declares
    that it shows this product and may be used in ads, the way confirming a product card declares the
    rights of its images (``intake_service._rights_for``). ``photo_not_found`` names photos the reading
    of these ideas did not find."""
    from app.library import service as library
    from app.products.intake_service import _rights_for
    from .product_references import candidates
    if not (photo_ids or rejected_photo_ids):
        return []
    found = {photo["photo_id"]: photo for photo in candidates(session, ctx, row)}
    unknown = [pid for pid in (*photo_ids, *rejected_photo_ids) if pid not in found]
    if unknown:
        raise MarketingError("photo_not_found", "要核对的照片不是读这个产品时找到的照片，请刷新后重试。",
                             params={"photo_ids": unknown})
    product = session.scalar(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
                                                         ProductRecord.id == row.product_id))
    lines, today = [], date.today()
    for decision, ids in (("verified", photo_ids), ("rejected", rejected_photo_ids)):
        for pid in ids:
            photo = found[pid]
            line = {"photo_id": pid, "decision": decision, "reference_object_id": photo["reference_object_id"],
                    "intake_id": photo["intake_id"], "image_id": photo["image_id"], "role": photo["role"],
                    "rank": photo["rank"]}
            if decision == "verified":
                try:
                    line["reference_version_id"] = library.promote_reference_in_transaction(
                        session, ctx, photo["reference_object_id"],
                        display_name=f"{((product.name if product else '') or 'Product')[:80]} · {photo['image_id']}",
                        rights=_rights_for(photo["intake_image"], photo["page_url"], today), today=today)
                except library.LibraryError as exc:
                    raise MarketingError("verified_product_unpublishable", "核对过的照片不能存进创意库，请刷新后重试。", 409,
                                         params={"reason": getattr(exc, "code", None) or "library_validation_failed"}) from exc
            lines.append(line)
    return lines


def verify(session, ctx, row, revision, *, fact_ids=(), appearance=(), findings=(), photo_ids=(), rejected_photo_ids=()):
    """Record one "check before download" (design doc §5.3): what the user confirms, nothing more.

    ``fact_ids``: facts the user read and confirms as true -- they become checked. ``appearance``: ads made
    without a product photo whose product the user confirms looks right. ``findings``: per ad, the
    findings of its newest check the user accepts as they are (``{asset_version_id, check_id,
    finding_ids}``); a check that changed since the user looked is refused. ``photo_ids`` (#199): photos
    the product's reading found that the user confirms show this product and may be used in ads;
    ``rejected_photo_ids``: photos the user says are not (the two never overlap). The checked facts and
    photos are published as the product's version. Returns the verification record."""
    _require_understanding(row)
    fact_ids, appearance = list(dict.fromkeys(fact_ids)), sorted(set(appearance))
    photo_ids, rejected_photo_ids = list(dict.fromkeys(photo_ids)), list(dict.fromkeys(rejected_photo_ids))
    if not (fact_ids or appearance or findings or photo_ids or rejected_photo_ids):
        raise MarketingError("verification_empty", "请至少核对一条事实、一张照片、一张广告的外观或一条检查结果。")
    understanding = row.understanding_json
    facts = {fact["id"]: fact for fact in understanding.get("facts", [])}
    missing = [fid for fid in fact_ids if fid not in facts]
    if missing:
        raise MarketingError("fact_not_found", "要核对的事实已不存在，请刷新后重试。", params={"fact_ids": missing})
    ads = _ads(session, ctx, row, [*appearance, *(item["asset_version_id"] for item in findings)])
    photographed = [asset_id for asset_id in appearance if _references(ads[asset_id][1])]
    if photographed:
        raise MarketingError("appearance_confirmation_not_needed", "这张广告用了产品照片：要核对的是照片，不是外观。",
                             params={"asset_version_ids": photographed})
    calls = ideas_checks.latest_by_asset(session, ctx, row.id)
    accepted = {}
    for item in findings:
        call = calls.get(item["asset_version_id"])
        if call is None or call.id != item["check_id"] or call.state != "succeeded":
            raise MarketingError("check_changed", "这张广告的检查结果已变化，请刷新后重新查看。", 409,
                                 params={"asset_version_id": item["asset_version_id"]})
        shown = {finding["check_id"] for finding in ideas_checks.projection(call)["findings"]}
        unknown = sorted(set(item["finding_ids"]) - shown)
        if unknown:
            raise MarketingError("finding_not_found", "检查结果里没有这条需要处理的发现，请刷新后重试。",
                                 params={"asset_version_id": item["asset_version_id"], "finding_ids": unknown})
        accepted.setdefault((item["asset_version_id"], call.id), set()).update(item["finding_ids"])
    document = {**understanding, "facts": [{**fact, "confirmation": "confirmed"} if fact["id"] in fact_ids else fact
                                           for fact in understanding.get("facts", [])]}
    # A checked fact still needs its quote in the source material.
    service.validate_understanding(row, Understanding.model_validate(document))
    photos = _photo_decisions_to_record(session, ctx, row, photo_ids, rejected_photo_ids)
    service.advance(session, ctx, row, revision, "verified", understanding_json=document)
    content = {"facts": [{"id": fid, "hash": fact_hash(facts[fid])} for fid in fact_ids], "appearance": appearance,
               "findings": [{"asset_version_id": asset_id, "check_id": call_id, "finding_ids": sorted(ids)}
                            for (asset_id, call_id), ids in sorted(accepted.items())],
               **({"photos": photos} if photos else {})}
    now, actor = utcnow(), _actor(ctx)
    pending = [{**line, "actor_id": actor, "decided_at": _stamp(now)} for line in photos]
    record = Record(id=new_id("mie"), workspace_id=ctx.workspace_id, project_id=row.id, kind="verification",
                    project_revision=row.revision, actor_id=actor, content_json=content, content_hash=digest(content),
                    library_version_id=publish_verified(session, ctx, row, pending=pending), created_at=now)
    session.add(record)
    append_event(session, event_type="marketing.ideas_verified", resource_type="marketing_project", resource_id=row.id,
                 details={"record_id": record.id, "revision": row.revision, "fact_ids": fact_ids, "appearance": appearance,
                          "findings": content["findings"], "photo_ids": photo_ids, "rejected_photo_ids": rejected_photo_ids,
                          "library_version_id": record.library_version_id})
    session.flush()
    return record


def assert_exportable(session, ctx, row, asset_version_ids):
    """Every one of these ads may be exported now, or ``ads_not_exportable`` naming each ad's reasons."""
    ads = _ads(session, ctx, row, asset_version_ids)
    calls = ideas_checks.latest_by_asset(session, ctx, row.id)
    result = statuses(session, ctx, row, calls=calls)
    refused = [{"asset_version_id": asset_id, "status": result[asset_id]["status"], "reasons": result[asset_id]["reasons"]}
               for asset_id in asset_version_ids if result[asset_id]["status"] != "exportable"]
    if refused:
        raise MarketingError("ads_not_exportable", "这些广告还不能下载：先核对它们用到的事实、外观和检查结果，需要重出的请先重出。",
                             409, params={"ads": refused})
    return ads, calls, result


def download_intent(session, ctx, row, asset_version_ids):
    """Hand out exactly these ads (design doc §5.3): one download intent each, recorded as one download.

    Refused as a whole unless every ad is exportable (``ads_not_exportable``). The record keeps what each
    ad stood on: the facts it cites, the check it passed and the image's own hash."""
    from app.generation.asset_queries import AssetBlobError, resolve_asset_blob_ref
    from app.platform.download_intents import create_download_intent_record
    service.ensure_active(session, ctx)
    ads, calls, result = assert_exportable(session, ctx, row, asset_version_ids)
    downloaded = []
    for asset_id in asset_version_ids:
        try:
            ref = resolve_asset_blob_ref(session, ctx, asset_id)
        except AssetBlobError:
            raise MarketingError("output_unavailable", "这张广告暂时没有可下载的文件。", 409,
                                 params={"asset_version_id": asset_id}) from None
        intent = create_download_intent_record(session, intent_id=new_id("di"), owner_kind="asset_version", owner_id=asset_id,
            committed_blob_ref_key=ref.committed_key, committed_blob_ref_version=ref.storage_version_id)
        downloaded.append({"asset_version_id": asset_id, "intent_id": intent.id, "content_sha256": ads[asset_id][0].content_sha256,
                           "cited_fact_ids": result[asset_id]["cited_fact_ids"], "check_id": calls[asset_id].id})
    content = {"ads": downloaded, "understanding_hash": digest(row.understanding_json)}
    record = Record(id=new_id("mie"), workspace_id=ctx.workspace_id, project_id=row.id, kind="download",
                    project_revision=row.revision, actor_id=_actor(ctx), content_json=content, content_hash=digest(content),
                    created_at=utcnow())
    session.add(record)
    append_event(session, event_type="marketing.ideas_downloaded", resource_type="marketing_project", resource_id=row.id,
                 details={"record_id": record.id, "revision": row.revision, "asset_version_ids": list(asset_version_ids)})
    session.flush()
    return record


def download_record(session, ctx, row, record_id: str):
    record = session.scalar(select(Record).where(Record.workspace_id == ctx.workspace_id, Record.project_id == row.id,
                                                 Record.id == record_id, Record.kind == "download"))
    if record is None:
        raise MarketingError("download_not_found", "找不到这次下载。", 404)
    return record


def download_grants(session, ctx, row, record) -> list[dict]:
    """What signing each download of a record needs: the blob, the file name and the seconds the intent has left.

    Resolved again on every call, a replay included, so a download whose file went away fails closed."""
    from app.generation.asset_queries import AssetBlobError, resolve_asset_blob_ref
    from app.generation.preview import _as_aware
    from app.learnings.codes import asset_code
    from app.platform.blob_store import extension_for_mime
    from app.platform.download_intents import get_download_intent
    combinations = {version.id: (ideas.intent_of(recipe).get("combination") or {}).get("id", "")
                    for version, recipe in ideas.project_ads(session, ctx, row)}
    grants = []
    for item in record.content_json["ads"]:
        intent = get_download_intent(session, item["intent_id"])
        try:
            ref = resolve_asset_blob_ref(session, ctx, item["asset_version_id"])
        except AssetBlobError:
            raise MarketingError("output_unavailable", "这张广告暂时没有可下载的文件。", 409,
                                 params={"asset_version_id": item["asset_version_id"]}) from None
        expiry = _as_aware(intent.expires_at)
        remaining = int((expiry - utcnow()).total_seconds())
        if remaining <= 0:
            raise MarketingError("download_expired", "下载链接已过期，请重新下载。", 410)
        grants.append({"asset_version_id": item["asset_version_id"], "combination_id": combinations.get(item["asset_version_id"], ""),
                       "intent_id": intent.id, "expires_at": expiry.isoformat(), "ref": ref, "remaining": remaining,
                       # The printable code a seller copies into the ad name (#166), so a report can be matched later.
                       "filename": f"ad-{asset_code(ctx.workspace_id, item['asset_version_id'])}"
                                   f".{extension_for_mime(ref.content_type_verified)}"})
    return grants
