"""Ads-first ideas (ADR 0018): one product's buying claims and ad drafts, before any test settings.

An ads-first project is a ``MarketingProjectRecord`` with ``flow = "ads_first"``. Its states are only
``input``, ``understanding_draft`` and ``ideas_ready``, its ``plan_json`` stays empty in step 1, and it
always has a product. Its ads are the generated images whose recipe intent names the project and the
ads-first flow (design D6); no round is involved. The plan-first endpoints never return these projects.
"""
from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import select

from app.adapters.persistence.models import (AssetVersionRecord, GenerationRunRecord, LibraryEntryRecord, ProductRecord,
                                            new_id, utcnow)
from app.generation.models import GenerationRecipeSnapshotRecord, RunItemRecord
from app.generation.selection import SelectionError
from app.platform.audit import append_event
from . import service
from .models import MarketingAdKeepRecord, MarketingProjectRecord

FLOW = "ads_first"
#: The first batch (step-1 plan, Global Constraints): claims, ways of telling per claim, ads per way.
FIRST_BATCH = {"angles": 3, "expressions": 2, "combinations": 1}
#: States from which ideas may be drafted: an understanding exists, checked or not (ADR 0018 §1).
DRAFTABLE = ("understanding_draft", "ideas_ready")


def _product_anchor(session, ctx, product_id):
    """The workspace product an ads-first project hangs on, given its ``prod_`` id or its Library entry id.

    The first batch builds ideas on a product draft that is published only when the user checks it (D3),
    so a draft product is accepted; an archived one is not.
    """
    product = session.scalar(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
        (ProductRecord.id == product_id) | (ProductRecord.product_profile_entry_id == product_id))
        .order_by(ProductRecord.product_profile_entry_id.is_(None), ProductRecord.id).limit(1))
    if product is not None:
        entry = session.scalar(select(LibraryEntryRecord).where(LibraryEntryRecord.workspace_id == ctx.workspace_id,
            LibraryEntryRecord.id == product.product_profile_entry_id)) if product.product_profile_entry_id else None
        if entry is None or entry.lifecycle == "active":
            return product.id
    else:
        try:  # a published Library product that has no product anchor yet gets one
            return service.product_card(session, ctx, product_id)[0]
        except SelectionError:
            pass
    raise service.MarketingError("product_not_found", "找不到这个产品。", 404)


def create_ideas_project(session, ctx, *, product_id: str, source_text: str, market: str, ad_language: str):
    service.ensure_active(session, ctx)
    anchor = _product_anchor(session, ctx, product_id)
    if not source_text.strip() or len(source_text) > 16000:
        raise service.MarketingError("source_invalid", "请粘贴不超过 16000 字的产品资料。", params={"limit": 16000})
    row = MarketingProjectRecord(id=new_id("mp"), workspace_id=ctx.workspace_id, product_id=anchor, flow=FLOW,
        source_text=source_text.strip(), market=market.strip(), ad_language=ad_language, revision=1, state="input")
    session.add(row)
    session.flush()
    service.checkpoint(session, ctx, row, "created")
    return row


def get_ideas_project(session, ctx, project_id: str):
    return service.get_project(session, ctx, project_id, flow=FLOW)


def project_ads(session, ctx, row):
    """This project's ads, oldest first: images whose recipe intent names the project and the ads-first flow."""
    return _ads_by_project(session, ctx, [row.id])[row.id]


def _ads_by_project(session, ctx, project_ids):
    """``project_ads`` of several projects with a fixed number of queries (the list reads every row's counts).

    An ad is a generated image whose recipe intent names the project (a correction or an image tool of an
    ad included), a crop or rotation of an ad, or an uploaded image placed under an ad (#200). The last two
    carry a recipe-like record of the intent they stand on (``ideas_revisions``)."""
    from .ideas_revisions import edited_ads, uploaded_ads
    records = session.execute(select(AssetVersionRecord, GenerationRecipeSnapshotRecord)
        .join(RunItemRecord, (RunItemRecord.id == AssetVersionRecord.producing_run_item_id) & (RunItemRecord.workspace_id == ctx.workspace_id))
        .join(GenerationRunRecord, (GenerationRunRecord.id == RunItemRecord.generation_run_id) & (GenerationRunRecord.workspace_id == ctx.workspace_id))
        .join(GenerationRecipeSnapshotRecord, (GenerationRecipeSnapshotRecord.id == GenerationRunRecord.recipe_snapshot_id) & (GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id))
        .where(AssetVersionRecord.workspace_id == ctx.workspace_id)
        .where(GenerationRecipeSnapshotRecord.canonical_json['selection']['marketing_intent']['project_id'].as_string().in_(project_ids))
        .order_by(AssetVersionRecord.created_at, AssetVersionRecord.id)).all() if project_ids else []
    ads = {project_id: [] for project_id in project_ids}
    for version, recipe in records:
        intent = _intent(recipe)
        if intent.get("project_id") in ads and intent.get("flow") == FLOW:
            ads[intent["project_id"]].append((version, recipe))
    for others in (edited_ads(session, ctx, project_ids), uploaded_ads(session, ctx, project_ids)):
        for project_id, items in others.items():
            for version, recipe in items:
                intent = _intent(recipe)
                if intent.get("project_id") == project_id and intent.get("flow") == FLOW:
                    ads[project_id].append((version, recipe))
    for project_id in ads:
        ads[project_id].sort(key=lambda pair: (pair[0].created_at, pair[0].id))
    return ads


def _intent(recipe):
    return (recipe.canonical_json or {}).get("selection", {}).get("marketing_intent") or {}


#: The ``marketing_intent`` a generation recipe froze, for other modules (the ideas checks, #197).
intent_of = _intent


def _keeps(session, ctx, row):
    return _keeps_by_project(session, ctx, [row.id])[row.id]


def _keeps_by_project(session, ctx, project_ids):
    keeps = {project_id: {} for project_id in project_ids}
    for mark in session.scalars(select(MarketingAdKeepRecord).where(MarketingAdKeepRecord.workspace_id == ctx.workspace_id,
            MarketingAdKeepRecord.project_id.in_(project_ids))).all() if project_ids else []:
        keeps[mark.project_id][mark.asset_version_id] = mark.kept
    return keeps


def set_kept(session, ctx, row, asset_version_id: str, kept: bool) -> None:
    service.ensure_active(session, ctx)
    if asset_version_id not in {version.id for version, _ in project_ads(session, ctx, row)}:
        raise service.MarketingError("ad_not_found", "这张广告不属于这些广告想法。", 404)
    # Serialize marks on one project so two first marks of the same ad never race on the unique key.
    session.execute(select(MarketingProjectRecord.id).where(MarketingProjectRecord.workspace_id == ctx.workspace_id,
        MarketingProjectRecord.id == row.id).with_for_update())
    mark = session.scalar(select(MarketingAdKeepRecord).where(MarketingAdKeepRecord.workspace_id == ctx.workspace_id,
        MarketingAdKeepRecord.project_id == row.id, MarketingAdKeepRecord.asset_version_id == asset_version_id))
    actor = ctx.initiator_id or ctx.executor
    if mark is None:
        session.add(MarketingAdKeepRecord(id=new_id("mak"), workspace_id=ctx.workspace_id, project_id=row.id,
            asset_version_id=asset_version_id, kept=kept, actor_id=actor, updated_at=utcnow()))
    else:
        mark.kept, mark.actor_id, mark.updated_at = kept, actor, utcnow()
    append_event(session, event_type="marketing.ad_kept", resource_type="marketing_project", resource_id=row.id,
                 details={"asset_version_id": asset_version_id, "kept": kept})
    session.flush()


def _ad(version, recipe, keeps, checks=None, statuses=None):
    from .ideas_revisions import origin
    combination = _intent(recipe).get("combination") or {}
    selection = (recipe.canonical_json or {}).get("selection") or {}
    # Derived, never stored (ADR 0018 §3, #198): draft, exportable or needs_redo, and why it is not exportable.
    derived = (statuses or {}).get(version.id) or {"status": "draft", "reasons": [], "cited_fact_ids": [],
                                                   "appearance_confirmed": False}
    made = origin(version, recipe)
    return {"asset_version_id": version.id, "combination_id": combination.get("id", ""),
            "angle_id": combination.get("angle_id", ""), "expression_id": combination.get("expression_id", ""),
            **derived, "kept": keeps.get(version.id, False),
            # How the image came to be and, for a new version of an ad, the version it was made from (#200).
            "origin": made,
            "source_asset_version_id": version.parent_version_id if made in ("corrected", "image_tool", "image_edit") else None,
            # The photos the request sent (design §5.2): an ad made without one is marked as such. A photo is
            # its content hash, the ``IdeasPhoto.photo_id`` its checking is read from (#199).
            "used_product_photo": bool(selection.get("product_references")),
            "photo_ids": [ref.get("content_sha256", "") for ref in selection.get("product_references") or []],
            "created_at": version.created_at.isoformat() if version.created_at else "",
            # The newest automatic check of this image (#197): only what needs attention.
            "check": (checks or {}).get(version.id)}


#: Run item states of an image not made yet, as ``PendingAd.state``; a succeeded, reused or cancelled item is not pending.
PENDING_STATES = {"pending": "queued", "queued": "queued", "paused": "queued",
                  "running": "running", "qc_pending": "running", "uncertain": "running", "failed": "failed"}


def pending_ads(session, ctx, row) -> list[dict]:
    """Images of the current ideas still being made, oldest first, and the ads whose latest attempt failed.

    An attempt that produced an image is an ad (``project_ads``), never pending, even while the image is
    being checked or after a check stopped it. A failed attempt is reported only while it is the newest
    attempt at its ad: trying the ad again replaces it. Attempts at content the ideas no longer hold are
    left out: redrafted ideas reuse combination ids, and nothing was made for the new content yet."""
    produced = (select(AssetVersionRecord.id).where(AssetVersionRecord.workspace_id == ctx.workspace_id,
                                                     AssetVersionRecord.producing_run_item_id == RunItemRecord.id)
                .exists().label("produced"))
    records = session.execute(select(RunItemRecord.state, produced, GenerationRecipeSnapshotRecord)
        .join(GenerationRunRecord, (GenerationRunRecord.id == RunItemRecord.generation_run_id) & (GenerationRunRecord.workspace_id == ctx.workspace_id))
        .join(GenerationRecipeSnapshotRecord, (GenerationRecipeSnapshotRecord.id == GenerationRunRecord.recipe_snapshot_id) & (GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id))
        .where(RunItemRecord.workspace_id == ctx.workspace_id)
        .where(GenerationRecipeSnapshotRecord.canonical_json['selection']['marketing_intent']['project_id'].as_string() == row.id)
        .order_by(RunItemRecord.created_at, RunItemRecord.id)).all()
    current = {}

    def still_current(intent, combination_id):
        if combination_id not in current:
            try:
                current[combination_id] = _content(ideas_intent(row, combination_id))
            except LookupError:
                current[combination_id] = None
        return current[combination_id] is not None and current[combination_id] == _content(intent)

    pending, latest = [], {}
    for state, made, recipe in records:
        intent = _intent(recipe)
        combination_id = (intent.get("combination") or {}).get("id", "")
        if intent.get("flow") != FLOW or not still_current(intent, combination_id):
            continue
        state = "succeeded" if made else state
        latest[combination_id] = state
        if PENDING_STATES.get(state) in ("queued", "running"):
            pending.append({"combination_id": combination_id, "state": PENDING_STATES[state]})
    return pending + [{"combination_id": cid, "state": "failed"} for cid, state in latest.items() if state == "failed"]


def ideas_dict(session, ctx, row) -> dict:
    """The ``IdeasProjectDetail`` of an ads-first project."""
    from . import ideas_checks, ideas_export
    from .first_batch import summaries
    from .ideas_revisions import edit_summaries
    call = service.latest_call(session, ctx, row.id)
    keeps = _keeps(session, ctx, row)
    ads = project_ads(session, ctx, row)
    calls = ideas_checks.latest_by_asset(session, ctx, row.id)
    checks = {asset_id: ideas_checks.projection(check) for asset_id, check in calls.items()}
    photos = ideas_export.photo_list(session, ctx, row, ads)
    states = ideas_export.photo_states(session, ctx, row)
    statuses = ideas_export.statuses(session, ctx, row, ads, calls, states)
    return {"id": row.id, "revision": row.revision, "state": row.state, "product_id": row.product_id,
            "market": row.market, "ad_language": row.ad_language, "understanding": row.understanding_json,
            "ideas": row.ideas_json,
            "latest_call": call_summary(call) if call else None,
            "ads": [_ad(version, recipe, keeps, checks, statuses) for version, recipe in ads],
            "pending": pending_ads(session, ctx, row), "first_batch": summaries(session, ctx, [row.id]).get(row.id),
            "image_edits": edit_summaries(session, ctx, row),
            # The photos the product's reading found and whether the user checked each (#199).
            "photos": photos}


def call_summary(call) -> dict:
    """The ``IdeasCallSummary`` of a drafting call: for an addition (#200), what it adds."""
    result = service.call_dict(call, include_maximum_cost=True)
    addition = (call.input_json or {}).get("addition")
    if call.kind == "ideas" and addition:
        result["addition"] = {"kind": addition["kind"], "angle_id": addition.get("angle_id")}
    return result


#: The parts of an intent that come from the ideas and shaped the draft's image prompt: an ads-first draft
#: stays valid while these equal the current ideas, whatever else changed (#191).
CONTENT_KEYS = ("combination", "proposition", "expression", "ad_language", "style_variation", "style", "brand_kit")


def ideas_intent(row, combination_id: str) -> dict:
    """The ``marketing_intent`` of one ad of these ideas (design D5): what it says and what it stands on.

    It records the product understanding as it is now, with each fact's checking status, and the facts
    the ad's claim and way of telling cite -- directly, or through a cited inference -- so S10 can tell
    whether the ad may be exported. Raises ``LookupError`` when the ideas have no such combination."""
    doc, combo, angle, expression, understanding = _ad_parts(row, combination_id)
    cited = _cited(angle, expression, understanding)
    return {"schema_version": 2, "flow": FLOW, "project_id": row.id, "plan_revision": row.revision,
            "ideas_hash": service.digest(row.ideas_json),
            "ad_language": doc.ad_language, "strategy_language": doc.strategy_language,
            # One visual style for every ad of the ideas: nothing about the style is being tested here.
            **({"style_variation": "held", "style": doc.style_notes} if doc.style_notes else {}),
            "brand_kit": doc.brand_kit.model_dump(mode="json") if doc.brand_kit else None,
            "combination": combo.model_dump(mode="json", exclude_unset=True), "proposition": angle.proposition,
            "expression": expression.model_dump(mode="json", exclude_none=True),
            "understanding": understanding.model_dump(mode="json"),
            "cited_fact_ids": [fact.id for fact in understanding.facts if fact.id in cited]}


def _ad_parts(row, combination_id: str):
    """The ideas, combination, claim, way of telling and understanding behind one ad, or ``LookupError``."""
    from .contracts import Understanding
    from .ideas_contracts import Ideas
    if row.state != "ideas_ready" or not row.ideas_json:
        raise LookupError(combination_id)
    doc = Ideas.model_validate(row.ideas_json)
    combo = next((c for c in doc.combinations if c.id == combination_id), None)
    if combo is None:
        raise LookupError(combination_id)
    angle = next(a for a in doc.angles if a.id == combo.angle_id)
    expression = next(e for e in angle.expressions if e.id == combo.expression_id)
    return doc, combo, angle, expression, Understanding.model_validate(row.understanding_json)


def _cited(angle, expression, understanding) -> set:
    """Every fact id an ad's claim and way of telling cite, directly or through a cited inference."""
    inferences = {i.id: i for i in understanding.inferences}
    return {*angle.fact_ids, *expression.evidence_basis.fact_ids,
            *(fact for iid in angle.inference_ids if iid in inferences for fact in inferences[iid].fact_ids)}


def citations(row, combination_id: str) -> set:
    """The fact ids one ad of these ideas cites now, including ids whose fact the user has since removed
    (the ideas are not rewritten when a fact goes: #198). Raises ``LookupError`` like ``ideas_intent``."""
    _, _, angle, expression, understanding = _ad_parts(row, combination_id)
    return _cited(angle, expression, understanding)


def _content(intent) -> dict:
    return {key: intent.get(key) for key in CONTENT_KEYS}


#: The content of an intent that decides whether an image is still the ad the ideas describe (#198).
content_of = _content


def _current_intent(project, intent):
    """The intent ``intent``'s ad has in ``project`` now, or ``SelectionError`` when its content changed."""
    try:
        current = ideas_intent(project, (intent.get("combination") or {}).get("id", ""))
    except LookupError:
        current = None
    if current is None or _content(current) != _content(intent):
        raise SelectionError("ideas_changed", "广告想法已变化，请按当前的主张重新准备这张广告。")
    return current


def selection_intent(session, ctx, project, link, draft) -> dict:
    """The intent a preview freezes for a draft prepared from these ideas (``resolve_selection``).

    The draft is valid while its ad's content still equals the ideas, not while the project revision
    stays the same: checking a fact or adding a claim later leaves it usable (#191).

    Its image prompt must be the one the application wrote from the ideas (#198): the intent is what an
    ad is later checked and exported against, so a prompt a client rewrote would make an image the intent
    does not describe. Changing what an ad says goes through the ideas, never through the draft."""
    from . import brand_kits
    selectors = draft.selectors_json or {}
    stored = selectors.get("marketing_intent") or {}
    # A draft edited to another product or another ad no longer matches the ideas: prepare it again.
    if project.product_id != draft.product_id or (stored.get("combination") or {}).get("id") != link.combination_id:
        raise SelectionError("ideas_changed", "广告想法已变化，请按当前的主张重新准备这张广告。")
    current = _current_intent(project, stored)
    doc, combo, *_ = _ad_parts(project, link.combination_id)
    try:
        kit = brand_kits.resolve_ref(session, ctx, doc.brand_kit) if doc.brand_kit else None
    except service.MarketingError as exc:
        raise SelectionError(exc.code, exc.detail) from None
    written = _prompt(combo, current, kit, logo=bool(selectors.get("brand_logo")),
                      references=bool(selectors.get("product_references")))
    if selectors.get("prompt") != written:
        raise SelectionError("ideas_prompt_changed", "这张广告的出图说明被改过。新开头的广告只按主张出图，请重新准备这张广告。")
    assert_photos_usable(session, ctx, project, selectors.get("product_references"))
    return current


def assert_photos_usable(session, ctx, project, references) -> None:
    """No new image is sent with a photo the user rejected for these ideas (#199): such a draft is
    prepared again on the current photos. Checked or not, any other photo may make a draft."""
    if not references:
        return
    from .ideas_export import photo_decisions
    decided = photo_decisions(session, ctx, project)
    if any((decided.get(ref.get("content_sha256")) or {}).get("decision") == "rejected" for ref in references):
        raise SelectionError("product_reference_changed", "这张广告用到的产品照片已被否定，请按当前的照片重新准备。")


def _prompt(combo, intent, kit, *, logo: bool, references: bool) -> str:
    """The image prompt the application writes for one ad of the ideas: nothing a client supplies."""
    from . import brand_kits
    from .product_references import REFERENCE_BRIEF
    brand = brand_kits.brief_lines(kit.content_json, logo_supplied=logo) if kit else ()
    return service.image_brief(combo, intent, brand=brand) + ("\n" + REFERENCE_BRIEF if references else "")


def check_intent(session, ctx, intent, references=()):
    """A frozen ads-first intent at submission and execution: its project exists in this workspace, is
    ads-first, and its ad's content still equals the ideas. A plan-first project never matches.
    ``references``: the photos the recipe froze, none of which the user may have rejected since (#199)."""
    try:
        project = get_ideas_project(session, ctx, intent.get("project_id", ""))
    except service.MarketingError:
        raise SelectionError("ideas_unavailable", "找不到关联的广告想法。") from None
    _current_intent(project, intent)
    assert_photos_usable(session, ctx, project, references)
    return project


def generation_drafts(session, ctx, row, revision, provider, model, format_config):
    """One generation draft per combination of the ideas, with nothing confirmed first (ADR 0018 §1, #191).

    Each draft carries its ad's ``ideas_intent`` and is linked to the project and combination, so its
    preview and submission go through the existing generation endpoints. Preparing again at the same
    revision returns the same drafts. The appearance references are the product's photos as they are at
    this revision (``product_references.for_ideas``, #199): checked ones first, then those the reading
    found that nobody checked yet, never a rejected one. A photo decision is a verification, which spends
    a revision, so new photos always mean new drafts."""
    service.ensure_active(session, ctx)
    call = service.latest_call(session, ctx, row.id)
    if call and call.state in ("queued", "running", "uncertain"):
        raise service.MarketingError("planning_in_progress", "请先等待起草完成或核对不确定调用。", 409)
    if row.revision != revision:
        raise service.MarketingError("stale_marketing_revision", "内容已更新，请刷新后重试。", 409)
    if row.state != "ideas_ready" or not row.ideas_json:
        raise service.MarketingError("ideas_not_ready", "请先起草广告主张。", 409)
    from app.generation.draft import create_draft
    from app.generation.models import GenerationDraftRecord
    from app.providers.router import get_provider
    from . import brand_kits
    from .ideas_contracts import Ideas
    from .models import MarketingGenerationLinkRecord
    from .product_references import for_ideas
    if model not in get_provider(provider).models:
        raise service.MarketingError("model_unavailable", "该供应商不支持所选模型。")
    doc = Ideas.model_validate(row.ideas_json)
    references = for_ideas(session, ctx, row)
    kit = brand_kits.resolve_ref(session, ctx, doc.brand_kit) if doc.brand_kit else None
    logo = brand_kits.logo_input(session, ctx, kit.content_json["logo"]) if kit and kit.content_json.get("logo") else None
    drafts = []
    for combo in doc.combinations:
        link = session.scalar(select(MarketingGenerationLinkRecord).where(
            MarketingGenerationLinkRecord.workspace_id == ctx.workspace_id, MarketingGenerationLinkRecord.project_id == row.id,
            MarketingGenerationLinkRecord.plan_revision == revision, MarketingGenerationLinkRecord.combination_id == combo.id))
        if link:
            # Only the settings the client chose can conflict. The photos and logo are the application's
            # choice: one checked or replaced since keeps the existing drafts, whose frozen references the
            # preview checks again, rather than blocking this revision for good.
            existing = session.get(GenerationDraftRecord, link.draft_id)
            if (existing.provider, existing.model, existing.format_json) != (provider, model, format_config):
                raise service.MarketingError("ideas_drafts_already_prepared",
                                             "这些广告已按其他出图设置准备过；请用相同的设置，或重新起草主张后再准备。", 409)
            drafts.append({"combination_id": combo.id, "draft_id": link.draft_id})
            continue
        if combo.image_brief is None:  # drafted ideas always have one (``checked_ideas``)
            raise service.MarketingError("image_brief_language_mismatch", "Write the English image brief for every ad.")
        intent = ideas_intent(row, combo.id)
        prompt = _prompt(combo, intent, kit, logo=bool(logo), references=bool(references))
        if len(prompt) > 4000:
            raise service.MarketingError("ad_brief_too_long", "这张广告的画面说明太长，请重新起草主张。")
        draft = create_draft(session, ctx, product_id=row.product_id, provider=provider, model=model,
            execution_mode="mock" if provider == "mock" else "byok", combination_strategy={"mode": "single"},
            format_config=format_config, selectors={"prompt": prompt, "marketing_intent": intent,
                "product_references": references, "reference_actor_id": ctx.initiator_id or ctx.executor,
                **({"brand_logo": logo} if logo else {})})
        session.add(MarketingGenerationLinkRecord(id=new_id("mgl"), workspace_id=ctx.workspace_id, project_id=row.id,
                                                  plan_revision=revision, combination_id=combo.id, draft_id=draft.id))
        drafts.append({"combination_id": combo.id, "draft_id": draft.id})
    session.flush()
    return drafts


def prepared_drafts(session, ctx, row, revision) -> list[dict]:
    """The drafts prepared for these ideas at ``revision``, in the order of the ideas' combinations."""
    from .models import MarketingGenerationLinkRecord
    links = {link.combination_id: link.draft_id for link in session.scalars(select(MarketingGenerationLinkRecord).where(
        MarketingGenerationLinkRecord.workspace_id == ctx.workspace_id, MarketingGenerationLinkRecord.project_id == row.id,
        MarketingGenerationLinkRecord.plan_revision == revision)).all()}
    order = [c["id"] for c in (row.ideas_json or {}).get("combinations", [])]
    ordered = [cid for cid in order if cid in links] + sorted(cid for cid in links if cid not in order)
    return [{"combination_id": cid, "draft_id": links[cid]} for cid in ordered]


def ideas_summary(session, ctx, row) -> dict:
    """The ``IdeasProjectSummary`` of an ads-first project."""
    return ideas_summaries(session, ctx, [row])[0]


def ideas_summaries(session, ctx, rows) -> list[dict]:
    """``ideas_summary`` of several projects with a fixed number of queries, in the order given."""
    from .first_batch import summaries
    ids = [row.id for row in rows]
    ads, keeps = _ads_by_project(session, ctx, ids), _keeps_by_project(session, ctx, ids)
    batches = summaries(session, ctx, ids)
    product_ids = {row.product_id for row in rows}
    names = {p.id: p.name for p in session.scalars(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
                                                                              ProductRecord.id.in_(product_ids))).all()} if rows else {}
    result = []
    for row in rows:
        own = {version.id for version, _ in ads[row.id]}
        name = names.get(row.product_id) or (row.understanding_json or {}).get("product_name") or row.source_text[:80]
        result.append({"id": row.id, "revision": row.revision, "state": row.state, "product_id": row.product_id,
                       "product_name": name, "ad_count": len(own), "kept_count": sum(1 for ad in own if keeps[row.id].get(ad)),
                       "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                       "first_batch": batches.get(row.id)})
    return result


def start_ideas_planning(session, ctx, row, revision, kind, mode, quote_hash, strategy_language="en-US", addition=None):
    """Admit a drafting call of ads-first ideas: the product understanding, or the ideas drawn from it (#190).

    Neither needs a checked fact (ADR 0018 §1). The understanding keeps the plan-first checks on quotes and
    claims; the ideas need a drafted understanding. Both are ordinary paid planning calls: one job, one
    reservation, settled by the Worker, and an explicit retry carries the last rejected draft.

    ``addition`` (kind ``ideas`` only, #200) drafts one more claim, or one more way of telling an existing
    claim, instead of the whole ideas: see ``start_addition``."""
    if addition is not None:
        if kind != "ideas":
            raise service.MarketingError("addition_kind_invalid", "只能给广告想法追加主张或讲法。")
        return start_addition(session, ctx, row, revision, mode, quote_hash, addition)
    if kind == "understanding":
        call = service.start_planning(session, ctx, row, revision, "understanding", mode, quote_hash,
                                      ad_language=row.ad_language, strategy_language=strategy_language)
        # The new understanding comes back unchecked, so what this project had checked leaves the product's
        # published version now, when the user asks for it (#198). A call that fails leaves the facts
        # checked here and the version one step behind until the next check: never ahead of the facts.
        if any(fact.get("confirmation") == "confirmed" for fact in (row.understanding_json or {}).get("facts", [])):
            from . import ideas_export
            ideas_export.publish_verified(session, ctx, row, excluding=row.id)
        return call
    if row.state not in DRAFTABLE or not row.understanding_json:
        raise service.MarketingError("understanding_required", "请先生成或完善产品理解。")
    cap = service.planner_capability(session, ctx)
    if mode == "byok" and (not cap["available"] or quote_hash != cap["quote_hash"]):
        raise service.MarketingError("planner_unavailable", "策划凭据、报价或预算不可用，请刷新配置。")
    from . import brand_kits
    from .expression_catalog import planner_reference
    from .planner import system_prompt
    inputs = {"source_text": row.source_text, "understanding": row.understanding_json, "market": row.market,
              "ad_language": row.ad_language, "strategy_language": strategy_language,
              "expression_catalog": planner_reference()}
    brand_ref = brand_kits.default_ref(session, ctx, row)
    if brand_ref:
        inputs["brand_kit"] = brand_kits.qc_summary(brand_kits.resolve_ref(session, ctx, brand_ref))
    repair = service.planning_repair_context(session, ctx, row, "ideas")
    if repair:
        inputs["rejected_plan_feedback"] = repair

    def size():
        return len((system_prompt("ideas", inputs) + json.dumps(inputs, ensure_ascii=False)).encode("utf-8"))
    if repair and size() > 96000:
        repair.pop("rejected_plan", None)
    if size() > 96000:
        raise service.MarketingError("planning_input_too_large", "产品资料与结构化理解过长，请精简后再起草方案。")
    service.advance(session, ctx, row, revision, "planning_requested")
    return service.admit_call(session, ctx, row, "ideas", mode, cap, inputs)


#: The most ways of telling one claim may have, and the most claims and ads ideas may hold (``Angle``, ``Ideas``).
MAX_EXPRESSIONS, MAX_ANGLES, MAX_COMBINATIONS = 8, 8, 30
#: What one addition drafts: a claim with up to two ways of telling, one ad each; or one way of telling with one ad.
CLAIM_EXPRESSIONS = 2


def start_addition(session, ctx, row, revision, mode, quote_hash, addition):
    """Admit a drafting call that adds to the ideas, never rewriting them (#200, design doc §5.3).

    ``addition``: ``{kind: "claim" | "expression", angle_id, note, guiding_answers}``. A claim comes with up
    to two ways of telling it, one ad each; a way of telling comes with one ad, and its picture mechanism
    must differ from every way of telling the claim already has. The note (the user's own claim or way of
    telling) and the answers to guiding questions are the user's wishes, never product facts.

    The result is merged by the Worker under new ids, so no ad made so far changes content or status. It
    is an ordinary paid planning call (one job, one reservation, the confirmed planner quote) and an
    explicit retry carries the last refused addition of the same kind and claim. Refused while the
    product's first batch is still running: its own image step makes every ad the ideas hold."""
    from . import brand_kits
    from .expression_catalog import planner_reference
    from .first_batch import ENDED
    from .ideas_contracts import Ideas
    from .models import MarketingFirstBatchRecord
    from .planner import system_prompt
    if row.state != "ideas_ready" or not row.ideas_json:
        raise service.MarketingError("ideas_not_ready", "请先起草广告主张。", 409)
    batch = session.scalar(select(MarketingFirstBatchRecord).where(MarketingFirstBatchRecord.workspace_id == ctx.workspace_id,
                                                                   MarketingFirstBatchRecord.project_id == row.id))
    if batch is not None and batch.state not in ENDED:
        raise service.MarketingError("first_batch_running", "首批还没有完成，完成后再追加主张或讲法。", 409)
    doc = Ideas.model_validate(row.ideas_json)
    wanted = {"kind": addition["kind"], "angle_id": addition.get("angle_id"), "note": addition.get("note", ""),
              "guiding_answers": addition.get("guiding_answers", [])}
    ads = CLAIM_EXPRESSIONS if wanted["kind"] == "claim" else 1
    if wanted["kind"] == "claim":
        full = len(doc.angles) >= MAX_ANGLES
    else:
        angle = next((a for a in doc.angles if a.id == wanted["angle_id"]), None)
        if angle is None:
            raise service.MarketingError("angle_not_found", "找不到要追加讲法的主张。", 404)
        full = len(angle.expressions) >= MAX_EXPRESSIONS
    if full or len(doc.combinations) + ads > MAX_COMBINATIONS:
        raise service.MarketingError("ideas_full", "这些广告想法已经到上限，不能再追加。", 409,
                                     params={"angles": MAX_ANGLES, "expressions": MAX_EXPRESSIONS, "ads": MAX_COMBINATIONS})
    cap = service.planner_capability(session, ctx)
    if mode == "byok" and (not cap["available"] or quote_hash != cap["quote_hash"]):
        raise service.MarketingError("planner_unavailable", "策划凭据、报价或预算不可用，请刷新配置。")
    # The ideas keep one language pair and one brand baseline: an addition is written in theirs.
    inputs = {"source_text": row.source_text, "understanding": row.understanding_json, "market": row.market,
              "ad_language": doc.ad_language, "strategy_language": doc.strategy_language,
              "expression_catalog": planner_reference(), "ideas": row.ideas_json, "ideas_hash": service.digest(row.ideas_json),
              "addition": wanted}
    if doc.brand_kit:
        inputs["brand_kit"] = brand_kits.qc_summary(brand_kits.resolve_ref(session, ctx, doc.brand_kit))
    repair = _addition_repair(session, ctx, row, wanted)
    if repair:
        inputs["rejected_plan_feedback"] = repair

    def size():
        return len((system_prompt("ideas", inputs) + json.dumps(inputs, ensure_ascii=False)).encode("utf-8"))
    if repair and size() > 96000:
        repair.pop("rejected_plan", None)
    if size() > 96000:
        raise service.MarketingError("planning_input_too_large", "产品资料与结构化理解过长，请精简后再起草方案。")
    service.advance(session, ctx, row, revision, "planning_requested")
    return service.admit_call(session, ctx, row, "ideas", mode, cap, inputs)


def _addition_repair(session, ctx, row, wanted):
    """The last refused addition of the same kind and claim, on the same understanding and ideas, as an
    explicit correction; ``None`` otherwise (a whole-ideas rejection never feeds an addition, and back)."""
    previous = service.latest_call(session, ctx, row.id)
    added = (previous.input_json or {}).get("addition") if previous else None
    if (not added or previous.kind != "ideas" or previous.state != "failed" or previous.error_code != "planner_output_invalid"
            or (added["kind"], added.get("angle_id")) != (wanted["kind"], wanted["angle_id"])
            or previous.input_json.get("understanding") != row.understanding_json
            or previous.input_json.get("ideas") != row.ideas_json):
        return None
    output = previous.output_json or {}
    errors = [{"type": str(e.get("type", "invalid_plan"))[:128], "loc": list(e.get("loc", []))[:12],
               "msg": str(e.get("msg", e.get("type", "invalid_plan")))[:400]}
              for e in output.get("validation_errors", [])[:10] if isinstance(e, dict)]
    if not errors:
        return None
    feedback = {"policy": service.REPAIRS["ideas_addition"], "previous_call_id": previous.id, "validation_errors": errors}
    rejected = output.get("rejected_output")
    if isinstance(rejected, dict) and len(json.dumps(rejected, ensure_ascii=False).encode()) <= 16000:
        feedback["rejected_plan"] = rejected
    return feedback


def _fresh(prefix: str, used: set) -> str:
    """An id no ad of these ideas ever had: ``prefix`` and the smallest free number."""
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    used.add(f"{prefix}{n}")
    return f"{prefix}{n}"


def _used_ids(row) -> set:
    """Every id the ideas hold now; new ids also carry the call's own stem, so an id an earlier image froze is never reused."""
    doc = row.ideas_json or {}
    return {*(a["id"] for a in doc.get("angles", [])), *(e["id"] for a in doc.get("angles", []) for e in a.get("expressions", [])),
            *(c["id"] for c in doc.get("combinations", []))}


def added_ideas(row, data, inputs, call_id: str):
    """An addition as it may be merged into the ideas, and the merged ideas document; ``MarketingError`` when
    the draft is refused (the call is then ``planner_output_invalid``, nothing is written).

    New ids carry the call's own prefix, so no image made earlier -- even of ideas since redrafted -- ever
    names them, and no existing claim, way of telling or ad changes: every ad made so far keeps its
    content and status (#198). The draft is checked like a whole draft: languages, cited facts and
    inferences, the three elements of each way of telling, and no two ways of telling one claim with the
    same picture mechanism."""
    from copy import deepcopy
    from .contracts import Understanding
    from .competitor_review import compact
    from .ideas_contracts import ClaimAddition, ExpressionAddition, Ideas
    from .languages import english_brief_matches, script_matches
    from .expression_catalog import normalize_mechanism
    wanted = inputs["addition"]
    if row.state != "ideas_ready" or service.digest(row.ideas_json) != inputs["ideas_hash"]:
        raise service.MarketingError("ideas_changed", "广告想法在起草期间变了，请重新追加。")
    doc = Ideas.model_validate(row.ideas_json)
    understanding = Understanding.model_validate(row.understanding_json)
    facts, inferences = {f.id for f in understanding.facts}, {i.id for i in understanding.inferences}
    if wanted["kind"] == "claim":
        value = ClaimAddition.model_validate(data)
        angle, combinations = value.angle, value.combinations
        target = None
        expressions = angle.expressions
        if len(expressions) > CLAIM_EXPRESSIONS or len(combinations) != len(expressions):
            raise service.MarketingError("ideas_addition_too_large", "追加的主张最多 2 种讲法、每种讲法 1 张广告。",
                                         fields=_fields("angle.expressions"))
        if set(angle.fact_ids) - facts or set(angle.inference_ids) - inferences:
            raise service.MarketingError("plan_evidence_invalid", "角度引用了不存在的事实或推测。", fields=_fields("angle"))
        pairs = {(c.angle_id, c.expression_id) for c in combinations}
        if pairs != {(angle.id, e.id) for e in expressions}:
            raise service.MarketingError("marketing_combination_invalid", "每种讲法正好一张广告，广告引用这个主张自己的讲法。",
                                         fields=_fields("combinations"))
        path = "angle.expressions"
    else:
        value = ExpressionAddition.model_validate(data)
        target = next(a for a in doc.angles if a.id == wanted["angle_id"])
        angle, expressions, combinations = None, [value.expression], [value.combination]
        if value.combination.expression_id != value.expression.id:
            raise service.MarketingError("marketing_combination_invalid", "追加的广告要引用追加的讲法。", fields=_fields("combination"))
        path = "expression"
    seen = {normalize_mechanism(e.visual_mechanism) for e in (target.expressions if target else [])}
    for ei, expression in enumerate(expressions):
        where = f"{path}.{ei}" if angle is not None else path
        if set(expression.evidence_basis.fact_ids) - facts:
            raise service.MarketingError("plan_evidence_invalid", "表达的证据依据引用了不存在的事实。",
                                         fields=_fields(f"{where}.evidence_basis.fact_ids"))
        mechanism = normalize_mechanism(expression.visual_mechanism)
        if mechanism in seen:
            # The design rule (#200): a new way of telling never repeats the claim's picture mechanism.
            raise service.MarketingError("expressions_not_distinct",
                                         "同一主张下的讲法画面机制重复；请换一种画面机制。", fields=_fields(f"{where}.visual_mechanism"))
        seen.add(mechanism)
    for ci, combo in enumerate(combinations):
        where = f"combinations.{ci}" if angle is not None else "combination"
        if not all(script_matches(text, doc.ad_language) for text in (combo.headline, combo.body_copy)):
            raise service.MarketingError("copy_language_mismatch", "Ad headline and body copy must use the selected advertising language.",
                                         fields=_fields(f"{where}.headline", f"{where}.body_copy"))
        if not english_brief_matches(combo):
            raise service.MarketingError("image_brief_language_mismatch", "Write the English image brief for every ad.",
                                         fields=_fields(f"{where}.image_brief"))
    # New ids under the call's own prefix; the addition's own references follow them.
    used = _used_ids(row)
    stem = "x" + call_id.rsplit("_", 1)[-1][:8]
    angle_id = _fresh(f"a{stem}_", used) if angle is not None else target.id
    renamed = {e.id: _fresh(f"e{stem}_", used) for e in expressions}
    new_expressions = [e.model_copy(update={"id": renamed[e.id]}) for e in expressions]
    new_combinations = [c.model_copy(update={"id": _fresh(f"c{stem}_", used), "angle_id": angle_id,
                                              "expression_id": renamed[c.expression_id]}) for c in combinations]
    merged = deepcopy(row.ideas_json)
    if angle is not None:
        new_angle = angle.model_copy(update={"id": angle_id, "expressions": new_expressions, "competitor_overlap": []})
        merged["angles"].append(compact({"angles": [new_angle.model_dump(mode="json")]})["angles"][0])
        result = ClaimAddition(angle=new_angle, combinations=new_combinations)
    else:
        next(a for a in merged["angles"] if a["id"] == angle_id)["expressions"].append(new_expressions[0].model_dump(mode="json"))
        result = ExpressionAddition(expression=new_expressions[0], combination=new_combinations[0])
    merged["combinations"].extend(c.model_dump(mode="json") for c in new_combinations)
    try:
        Ideas.model_validate(merged)
    except ValueError as exc:
        raise service.MarketingError("ideas_full", "追加后超出广告想法的上限或关系无效。") from exc
    return result, merged


def _fields(*paths):
    return [f"body.ideas.{path}" for path in paths]


def checked_ideas(row, value, inputs):
    """Drafted ideas as they may be stored, or ``MarketingError``: the whole draft is refused, never trimmed.

    Design doc §6: a way of telling that repeats another, or lacks one of its three elements, is rewritten
    by the system before any image -- here, the call is ``planner_output_invalid`` and an explicit retry
    carries the errors. The project's market and the product's brand baseline are the application's."""
    from .contracts import BrandKitRef, Understanding
    from .languages import english_brief_matches, english_text, script_matches
    from .plan_review import indistinct_expressions
    if value.ad_language != inputs.get("ad_language") or value.strategy_language != inputs.get("strategy_language", "en-US"):
        raise service.MarketingError("planning_language_mismatch", "The plan must retain the requested languages.")
    pairs = Counter((c.angle_id, c.expression_id) for c in value.combinations)
    if (len(value.angles) > FIRST_BATCH["angles"] or any(len(a.expressions) > FIRST_BATCH["expressions"] for a in value.angles)
            or any(count > FIRST_BATCH["combinations"] for count in pairs.values())):
        raise service.MarketingError("ideas_first_batch_too_large",
                                     "首批最多 3 个主张、每个主张 2 种讲法、每种讲法 1 张广告。")
    understanding = Understanding.model_validate(row.understanding_json)
    facts, inferences = {f.id for f in understanding.facts}, {i.id for i in understanding.inferences}
    for ai, angle in enumerate(value.angles):
        if set(angle.fact_ids) - facts or set(angle.inference_ids) - inferences:
            raise service.MarketingError("plan_evidence_invalid", "角度引用了不存在的事实或推测。",
                                         fields=_fields(f"angles.{ai}"))
        for ei, expression in enumerate(angle.expressions):
            if set(expression.evidence_basis.fact_ids) - facts:
                raise service.MarketingError("plan_evidence_invalid", "表达的证据依据引用了不存在的事实。",
                                             fields=_fields(f"angles.{ai}.expressions.{ei}.evidence_basis.fact_ids"))
    duplicates = [f"angles.{ai}.expressions.{ei}.visual_mechanism"
                  for ai, _, members in indistinct_expressions(value) for ei, _ in members]
    if duplicates:
        raise service.MarketingError("expressions_not_distinct",
                                     "同一角度下的表达画面机制重复；请补充不同的讲法与画面机制。", fields=_fields(*duplicates))
    for ci, combo in enumerate(value.combinations):
        if not all(script_matches(text, value.ad_language) for text in (combo.headline, combo.body_copy)):
            raise service.MarketingError("copy_language_mismatch",
                                         "Ad headline and body copy must use the selected advertising language.",
                                         fields=_fields(f"combinations.{ci}.headline", f"combinations.{ci}.body_copy"))
        if not english_brief_matches(combo):
            raise service.MarketingError("image_brief_language_mismatch",
                                         "Write the English image brief for every ad.", fields=_fields(f"combinations.{ci}.image_brief"))
    if not english_text(value.style_notes):
        raise service.MarketingError("image_brief_language_mismatch", "Write the visual style notes in English for the image model.",
                                     fields=_fields("style_notes"))
    brand = inputs.get("brand_kit")
    return value.model_copy(update={"market": row.market,
                                    "brand_kit": BrandKitRef.model_validate(brand["ref"]) if brand else None})


EXAMPLE_CLAIMS = (
    (("Understand the product value", "清楚理解产品价值"),
     (("Usage demonstration", "使用情境演示", "live_connection_demo",
       "Show one real use step by step so the benefit is observed, not asserted.", "逐步展示一次真实使用，让好处被看见而不是被宣称。"),
      ("Problem and solution", "问题与解决过程", "scenario_story",
       "Tell a short situation where the problem appears and the product resolves it.", "讲一个问题出现、产品解决问题的具体情境。"))),
    (("Address purchase concerns", "减少购买顾虑"),
     (("Questions and answers", "疑问问答", "qa_objection",
       "Ask the buyer's hesitation as a question and answer it with documented facts.", "把购买前的疑虑写成问题，用资料中的事实回答。"),
      ("Product details", "产品细节说明", "feature_closeup",
       "Zoom into the parts that deliver the benefit and label each with one documented fact.", "放大实现好处的部位，每处只标注一条有依据的事实。"))),
    (("Know where it fits", "知道适合哪里"),
     (("Usage boundaries", "适用边界解读", "fit_guide",
       "Show who and which situations it suits, and where it does not.", "说明适合谁、适合什么情境，以及不适合的情况。"),
      ("Everyday situation", "日常情境", "scenario_story",
       "Follow one ordinary day in which the product is simply there when needed.", "跟随普通的一天，产品在需要时就在那里。"))),
)
EXAMPLE_COPY = {"en": ("Explore the product", "Explore actual uses and suitable conditions."),
                "zh": ("了解产品用途", "了解产品的实际用途与适用条件。"),
                "ja": ("製品を知る", "製品の用途と適切な使用条件をご確認ください。"),
                "ko": ("제품을 알아보세요", "제품의 용도와 적절한 사용 조건을 확인하세요.")}


def example(inputs):
    """Controlled Mock ideas: the first batch's fixed shape from the drafted understanding, never a fallback for paid AI."""
    from .planner import EXAMPLE_STYLE
    zh = inputs.get("strategy_language", "en-US") == "zh-CN"
    ad_language = inputs.get("ad_language", "en-US")
    headline, body = EXAMPLE_COPY.get(ad_language.split("-")[0].lower(), EXAMPLE_COPY["en"])
    fact_ids = [f["id"] for f in inputs["understanding"]["facts"]][:3]
    gap = "" if fact_ids else ("资料中还没有可引用的事实。" if zh else "The source has no fact to cite yet.")
    angles, combinations = [], []
    for n, ((claim_en, claim_zh), tellings) in enumerate(EXAMPLE_CLAIMS, 1):
        claim = claim_zh if zh else claim_en
        angle = {"id": f"a{n}", "proposition": claim, "fact_ids": fact_ids, "expressions": [],
                 "rationale": "仅用于验证流程的示例假设。" if zh else "An unvalidated hypothesis for the controlled workflow example."}
        for j, (name_en, name_zh, mechanism, telling_en, telling_zh) in enumerate(tellings, 1):
            eid = f"e{n}_{j}"
            angle["expressions"].append({"id": eid, "name": name_zh if zh else name_en,
                "description": f"用{name_zh}讲清主张。" if zh else f"Explain the proposition through {name_en.lower()}.",
                "fit_reason": "示例建议，需核对产品适用性。" if zh else "A proposed way to explain this claim; confirm product applicability.",
                "how_to_tell": telling_zh if zh else telling_en, "visual_mechanism": mechanism,
                "evidence_basis": {"fact_ids": fact_ids, "gap": gap}})
            combinations.append({"id": f"c{n}_{j}", "angle_id": angle["id"], "expression_id": eid,
                "elements": {"scene": "Actual product usage environment"}, "headline": headline, "body_copy": body,
                "visual_brief": f"Show the product clearly using {name_en.lower()}, without unsupported claims.",
                "image_brief": {"proposition": claim_en, "expression": name_en, "hold_constant": ["Same product identity"],
                                "checks": ["Accurate product appearance", "No unsupported claims"]},
                "checks": ["产品外观与资料一致"] if zh else ["Product appearance matches the supplied facts"]})
        angles.append(angle)
    return {"market": inputs.get("market") or "", "ad_language": ad_language,
            "strategy_language": inputs.get("strategy_language", "en-US"), "angles": angles,
            "combinations": combinations, "style_notes": EXAMPLE_STYLE}


def example_addition(inputs):
    """Controlled Mock addition (#200): a claim with two ways of telling, or one more way of telling a claim,
    each with a picture mechanism the claim does not use yet. Never a fallback for paid AI."""
    from .expression_catalog import ENTRIES, normalize_mechanism
    zh = inputs.get("strategy_language", "en-US") == "zh-CN"
    ad_language = inputs.get("ad_language", "en-US")
    headline, body = EXAMPLE_COPY.get(ad_language.split("-")[0].lower(), EXAMPLE_COPY["en"])
    fact_ids = [f["id"] for f in inputs["understanding"]["facts"]][:3]
    gap = "" if fact_ids else ("资料中还没有可引用的事实。" if zh else "The source has no fact to cite yet.")
    wanted = inputs["addition"]
    taken = set()
    if wanted["kind"] == "expression":
        angle = next(a for a in inputs["ideas"]["angles"] if a["id"] == wanted["angle_id"])
        taken = {normalize_mechanism(e["visual_mechanism"]) for e in angle["expressions"]}
    entries = [entry for entry in ENTRIES if normalize_mechanism(entry["visual_mechanism"]) not in taken]

    def expression(eid, entry):
        name = entry["name"]
        return {"id": eid, "name": name, "description": f"用{name}讲清主张。" if zh else f"Explain the proposition through {name.lower()}.",
                "fit_reason": "示例建议，需核对产品适用性。" if zh else "A proposed way to explain this claim; confirm product applicability.",
                "how_to_tell": entry["how_to_tell"], "visual_mechanism": entry["visual_mechanism"],
                "evidence_basis": {"fact_ids": fact_ids, "gap": gap}}

    def combination(cid, aid, eid, entry, claim_en):
        return {"id": cid, "angle_id": aid, "expression_id": eid, "elements": {"scene": "Actual product usage environment"},
                "headline": headline, "body_copy": body,
                "visual_brief": f"Show the product clearly using {entry['name'].lower()}, without unsupported claims.",
                "image_brief": {"proposition": claim_en, "expression": entry["name"], "hold_constant": ["Same product identity"],
                                "checks": ["Accurate product appearance", "No unsupported claims"]},
                "checks": ["产品外观与资料一致"] if zh else ["Product appearance matches the supplied facts"]}

    if wanted["kind"] == "claim":
        claim_en = "Another reason to choose it"
        claim = wanted.get("note") or ("另一个购买理由" if zh else claim_en)
        chosen = entries[:CLAIM_EXPRESSIONS]
        return {"angle": {"id": "new", "proposition": claim, "fact_ids": fact_ids,
                          "rationale": "仅用于验证流程的示例假设。" if zh else "An unvalidated hypothesis for the controlled workflow example.",
                          "expressions": [expression(f"n{j}", entry) for j, entry in enumerate(chosen, 1)]},
                "combinations": [combination(f"k{j}", "new", f"n{j}", entry, claim_en) for j, entry in enumerate(chosen, 1)]}
    angle = next(a for a in inputs["ideas"]["angles"] if a["id"] == wanted["angle_id"])
    combo = next(c for c in inputs["ideas"]["combinations"] if c["angle_id"] == angle["id"])
    claim_en = (combo.get("image_brief") or {}).get("proposition") or "The product's buying reason"
    return {"expression": expression("n1", entries[0]),
            "combination": combination("k1", angle["id"], "n1", entries[0], claim_en)}
