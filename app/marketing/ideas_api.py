"""Authenticated, idempotent v1 endpoints for ads-first ideas (ADR 0018, #189).

The plan-first endpoints in ``api.py`` never return these projects, and these never return plan-first ones.
"""
from datetime import datetime

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import and_, or_, select

from app.adapters.persistence.database import tenant_session
from app.generation.v1_router import _idempotency_key, _resource_command
from app.platform.etag import extract_if_match, set_etag
from app.platform.http_identity import request_tenant_context
from app.platform.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.platform.tenancy import TenantContext
from . import ideas, service
from .ideas_contracts import (AddIdeasPhoto, CreateIdeas, CreateIdeasImageEdit, DownloadIdeas, EditIdeasClaim,
                              EditIdeasUnderstanding, IdeasCallSummary, IdeasChecksQuote, IdeasDownload, IdeasFeedbackDraft,
                              IdeasGenerationDrafts, IdeasPhotoDownloadGrant, IdeasProjectDetail, IdeasProjectPage,
                              PlaceIdeasUpload, PrepareIdeasFeedback, PrepareIdeasGeneration, SetKept, StartIdeasChecks,
                              StartIdeasPlanning, VerifyIdeas)
from .models import MarketingProjectRecord, PlanningCallRecord

router = APIRouter(prefix="/marketing/ideas", tags=["marketing"])


@router.post("", operation_id="create_marketing_ideas", response_model=IdeasProjectDetail, status_code=201,
             openapi_extra={"x-marketai-idempotent": True})
def create_ideas(body: CreateIdeas, request: Request, response: Response,
                 ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Open ads-first ideas for a product of this workspace; drafting starts later (S2)."""
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.create", body.model_dump(mode="json"),
            lambda s: ideas.create_ideas_project(s, ctx, product_id=body.product_id, source_text=body.source_text,
                                                 market=body.market, ad_language=body.ad_language),
            lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.get("", operation_id="list_marketing_ideas", response_model=IdeasProjectPage)
def list_ideas(ctx: TenantContext = Depends(request_tenant_context), limit: int = Query(default=50, ge=1, le=100),
               cursor: str | None = Query(default=None, max_length=2048)):
    """This workspace's ads-first ideas, newest first."""
    filters = "marketing-ideas-created-v1"
    cls = MarketingProjectRecord
    with tenant_session(ctx) as session:
        query = select(cls).where(cls.workspace_id == ctx.workspace_id, cls.flow == ideas.FLOW)
        if cursor:
            try:
                position = decode_cursor(cursor, ctx.workspace_id, filters)
                if len(position) != 2 or not all(isinstance(v, str) for v in position) or not 1 <= len(position[1]) <= 64:
                    raise ValueError("invalid position")
                stamp = datetime.fromisoformat(position[0])
                query = query.where(or_(cls.created_at < stamp, and_(cls.created_at == stamp, cls.id < position[1])))
            except (InvalidCursorError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise service.MarketingError("invalid_cursor", "列表位置无效，请从第一页重新加载。", 400) from None
        rows = session.scalars(query.order_by(cls.created_at.desc(), cls.id.desc()).limit(limit + 1)).all()
        more, rows = len(rows) > limit, rows[:limit]
        return {"items": ideas.ideas_summaries(session, ctx, rows),
                "next_cursor": encode_cursor(ctx.workspace_id, filters, (rows[-1].created_at.isoformat(), rows[-1].id))
                if more else None}


@router.get("/{project_id}", operation_id="get_marketing_ideas", response_model=IdeasProjectDetail)
def get_ideas(project_id: str, response: Response, ctx: TenantContext = Depends(request_tenant_context)):
    with tenant_session(ctx) as session:
        row = ideas.get_ideas_project(session, ctx, project_id)
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.put("/{project_id}/ads/{asset_version_id}/kept", operation_id="set_marketing_ad_kept",
            response_model=IdeasProjectDetail, openapi_extra={"x-marketai-idempotent": True})
def set_ad_kept(project_id: str, asset_version_id: str, body: SetKept, request: Request, response: Response,
                ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Keep an ad of these ideas, or stop keeping it; each change is audited."""
    def keep(s):
        row = ideas.get_ideas_project(s, ctx, project_id)
        ideas.set_kept(s, ctx, row, asset_version_id, body.kept)
        return row
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.keep", body.model_dump(mode="json"), keep,
                                lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.post("/{project_id}/planning", operation_id="start_marketing_ideas_planning", response_model=IdeasCallSummary,
             status_code=202, openapi_extra={"x-marketai-idempotent": True})
def start_ideas_planning(project_id: str, body: StartIdeasPlanning, request: Request,
                         ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                         if_match: str | None = Header(default=None, alias="If-Match")):
    """Draft the product understanding, or the ideas from it; no fact needs checking first (ADR 0018, #190)."""
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def replay(s, resource):
            call = s.scalar(select(PlanningCallRecord).where(PlanningCallRecord.id == resource,
                                                           PlanningCallRecord.workspace_id == ctx.workspace_id))
            if call is None:
                raise service.MarketingError("planning_not_found", "找不到策划任务。", 404)
            return call
        call = _resource_command(session, ctx, request, key, "marketing.ideas.plan", body.model_dump(mode="json"),
            lambda s: ideas.start_ideas_planning(s, ctx, ideas.get_ideas_project(s, ctx, project_id), revision, body.kind,
                                                 body.execution_mode, body.quote_hash, body.strategy_language,
                                                 addition=body.addition.model_dump(mode="json") if body.addition else None),
            replay)
        return ideas.call_summary(call)


@router.post("/{project_id}/generation-drafts", operation_id="prepare_marketing_ideas_generation",
             response_model=IdeasGenerationDrafts, status_code=201, openapi_extra={"x-marketai-idempotent": True})
def prepare_ideas_generation(project_id: str, body: PrepareIdeasGeneration, request: Request,
                             ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                             if_match: str | None = Header(default=None, alias="If-Match")):
    """One generation draft per ad of these ideas; no fact or plan needs confirming first (ADR 0018, #191).

    Preview and submit each draft with ``create_generation_preview`` and ``submit_generation_run``; the
    images then appear under the ideas' ``ads`` (``pending`` while they are being made)."""
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        def prepare(s):
            row = ideas.get_ideas_project(s, ctx, project_id)
            ideas.generation_drafts(s, ctx, row, revision, body.provider, body.model,
                                    {"aspect_ratio": body.aspect_ratio, "image_size": body.image_size, "variants": body.variants})
            return row
        row = _resource_command(session, ctx, request, key, "marketing.ideas.prepare_generation", body.model_dump(mode="json"),
                                prepare, lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        return {"project_id": row.id, "revision": revision, "drafts": ideas.prepared_drafts(session, ctx, row, revision)}


@router.get("/{project_id}/checks/quote", operation_id="get_marketing_ideas_checks_quote", response_model=IdeasChecksQuote)
def get_ideas_checks_quote(project_id: str, execution_mode: Literal["byok", "mock"] = Query(default="byok"),
                           ctx: TenantContext = Depends(request_tenant_context)):
    """What checking the unchecked ads of these ideas costs at most: one single-image check per image (#197)."""
    from . import ideas_checks
    with tenant_session(ctx) as session:
        row = ideas.get_ideas_project(session, ctx, project_id)
        return ideas_checks.quote_and_inputs(session, ctx, row, execution_mode)[0]


@router.post("/{project_id}/checks", operation_id="start_marketing_ideas_checks", response_model=IdeasProjectDetail,
             status_code=202, openapi_extra={"x-marketai-idempotent": True})
def start_ideas_checks(project_id: str, body: StartIdeasChecks, request: Request, response: Response,
                       ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Check the ads the quote counted, against the intent each image was made from (#197).

    The result is a suggestion on each ad (``ads[].check``), never a quality approval; no round is needed.
    Without the workspace's key nothing is admitted (409 ``checks_unavailable``)."""
    from . import ideas_checks

    def check(s):
        row = ideas.get_ideas_project(s, ctx, project_id)
        ideas_checks.start(s, ctx, row, body.execution_mode, body.quote_hash)
        return row
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.check", body.model_dump(mode="json"), check,
                                lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.patch("/{project_id}/understanding", operation_id="edit_marketing_ideas_understanding",
              response_model=IdeasProjectDetail, openapi_extra={"x-marketai-idempotent": True})
def edit_ideas_understanding(project_id: str, body: EditIdeasUnderstanding, request: Request, response: Response,
                             ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                             if_match: str | None = Header(default=None, alias="If-Match")):
    """Change, add or remove what the AI read about the product; the ideas and their images stay (#198).

    A fact is checked only by ``verify_marketing_ideas``: a changed or new fact is unchecked, whatever the
    document says. An ad that cites a changed or removed fact becomes ``needs_redo``; other ads are untouched."""
    from . import ideas_export
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.edit_understanding", body.model_dump(mode="json"),
            lambda s: ideas_export.edit_understanding(s, ctx, ideas.get_ideas_project(s, ctx, project_id), revision,
                                                      body.understanding, body.source_addition),
            lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.post("/{project_id}/verification", operation_id="verify_marketing_ideas", response_model=IdeasProjectDetail,
             openapi_extra={"x-marketai-idempotent": True})
def verify_ideas(project_id: str, body: VerifyIdeas, request: Request, response: Response,
                 ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                 if_match: str | None = Header(default=None, alias="If-Match")):
    """Record what the user checked before a download (ADR 0018 §3, #198): facts, the look of ads made
    without a product photo, check findings accepted as they are, the product photos the user checks or
    rejects (#199), and the audience (#224).

    Only what is named here becomes confirmed; the checked facts and photos, and the confirmed audience, are
    published as the product's version. Ads whose three conditions now hold become ``exportable``; ads made
    with a rejected photo need making again."""
    from . import ideas_export
    revision = extract_if_match(request)

    def verify(s):
        row = ideas.get_ideas_project(s, ctx, project_id)
        ideas_export.verify(s, ctx, row, revision, fact_ids=body.fact_ids, appearance=body.appearance_confirmed,
                            findings=[item.model_dump(mode="json") for item in body.accepted_findings],
                            photo_ids=body.photo_ids, rejected_photo_ids=body.rejected_photo_ids,
                            audience=bool(body.audience_confirmed))
        return row
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.verify", body.model_dump(mode="json"), verify,
                                lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.patch("/{project_id}/claims/{angle_id}", operation_id="edit_marketing_ideas_claim", response_model=IdeasProjectDetail,
              openapi_extra={"x-marketai-idempotent": True})
def edit_ideas_claim(project_id: str, angle_id: str, body: EditIdeasClaim, request: Request, response: Response,
                     ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                     if_match: str | None = Header(default=None, alias="If-Match")):
    """Change one claim in the user's own words (#224): what it says, why it is worth trying, the facts it names.

    Free of charge. Only facts of the understanding can be named, and the words may state nothing the named
    facts do not (``claim_unsupported``). The claim's images made before need making again (``ideas_changed``);
    its copy and image instructions stay as they are. Refused while the first batch is still making ads."""
    from . import ideas_claims

    def edit(s):
        return ideas_claims.edit_claim(s, ctx, ideas.get_ideas_project(s, ctx, project_id), revision, angle_id,
                                       proposition=body.proposition, rationale=body.rationale, fact_ids=body.fact_ids)
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.edit_claim",
                                {"angle_id": angle_id, **body.model_dump(mode="json")}, edit,
                                lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.delete("/{project_id}/claims/{angle_id}", operation_id="delete_marketing_ideas_claim",
               response_model=IdeasProjectDetail, openapi_extra={"x-marketai-idempotent": True})
def delete_ideas_claim(project_id: str, angle_id: str, request: Request, response: Response,
                       ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key),
                       if_match: str | None = Header(default=None, alias="If-Match")):
    """Take one claim, its ways of telling and its ads out of the ideas (#224); at least two claims stay.

    The images made for it stay listed among ``ads`` and need making again (``ideas_changed``): none of them
    can be downloaded or exported."""
    from . import ideas_claims
    revision = extract_if_match(request)
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.delete_claim", {"angle_id": angle_id},
            lambda s: ideas_claims.delete_claim(s, ctx, ideas.get_ideas_project(s, ctx, project_id), revision, angle_id),
            lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.post("/{project_id}/feedback-drafts", operation_id="prepare_marketing_ideas_feedback",
             response_model=IdeasFeedbackDraft, status_code=201, openapi_extra={"x-marketai-idempotent": True})
def prepare_ideas_feedback(project_id: str, body: PrepareIdeasFeedback, request: Request,
                           ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Change one ad by making it again, without a round (#200): a correction from the user's note and the
    findings of its check, or a supplier image tool (upscale, background removal, erasing).

    Nothing is spent here: preview the draft with ``create_generation_preview`` (the maximum cost) and submit
    it with ``submit_generation_run``. The new image is a new version of the ad, listed in ``ads`` beside the
    old one, and a draft that needs its own check. A correction is made on the claim and the facts as they
    are now; an image tool keeps what the source stood on and is refused for an ad that needs redoing."""
    from . import ideas_revisions
    with tenant_session(ctx) as session:
        record = _resource_command(session, ctx, request, key, "marketing.ideas.feedback", body.model_dump(mode="json"),
            lambda s: ideas_revisions.prepare_feedback(s, ctx, ideas.get_ideas_project(s, ctx, project_id), body),
            lambda s, resource: ideas_revisions.get_feedback(s, ctx, ideas.get_ideas_project(s, ctx, project_id), resource))
        return ideas_revisions.feedback_draft(record)


@router.post("/{project_id}/image-edits", operation_id="create_marketing_ideas_image_edit", response_model=IdeasProjectDetail,
             status_code=202, openapi_extra={"x-marketai-idempotent": True})
def create_ideas_image_edit(project_id: str, body: CreateIdeasImageEdit, request: Request, response: Response,
                            ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Crop then rotate one ad, without a round (#200): free of charge, no reservation and no paid call.

    The Worker writes the new version (``image_edits`` says when); it is listed in ``ads`` beside the old one,
    keeps what the source stood on and needs its own check."""
    from . import ideas_revisions

    def edit(s):
        row = ideas.get_ideas_project(s, ctx, project_id)
        ideas_revisions.prepare_image_edit(s, ctx, row, body)
        return row
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.image_edit", body.model_dump(mode="json"), edit,
                                lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.post("/{project_id}/uploads", operation_id="place_marketing_ideas_upload", response_model=IdeasProjectDetail,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
def place_ideas_upload(project_id: str, body: PlaceIdeasUpload, request: Request, response: Response,
                       ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Place the user's own image under one ad of these ideas (#200, design doc §5.3).

    Upload it first with ``create_external_creatives`` for the same product, declaring the right to use it.
    It then appears in ``ads`` (``origin = "uploaded"``) and is kept, checked, verified and downloaded like
    a generated ad of that claim; it is made without a product photo, so its look needs confirming."""
    from . import ideas_revisions

    def place(s):
        row = ideas.get_ideas_project(s, ctx, project_id)
        ideas_revisions.place_upload(s, ctx, row, body.external_creative_id, body.combination_id)
        return row
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.upload", body.model_dump(mode="json"), place,
                                lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.post("/{project_id}/photos", operation_id="add_marketing_ideas_photo", response_model=IdeasProjectDetail,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
def add_ideas_photo(project_id: str, body: AddIdeasPhoto, request: Request, response: Response,
                    ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Add the user's own photo to the product of these ideas (#233, design doc §5.2).

    Upload it first like any reference (an Upload Intent of purpose ``reference_asset``, then
    ``create_library_reference_object``) and wait for its gate. Once released, it is listed in ``photos``
    (``source = "added"``) in every ads-first project of the product, drafts images and is checked or
    rejected like a photo the product's reading found; until the user checks it, its ads are drafts.
    409 ``photo_upload_checking`` while the gate is still checking it, ``photo_upload_rejected`` (with the
    gate's ``failure_code``) when it did not pass, ``photo_already_listed`` when the product has it already."""
    from . import ideas_photos

    def add(s):
        row = ideas.get_ideas_project(s, ctx, project_id)
        ideas_photos.add(s, ctx, row, body.reference_object_id, body.role)
        return row
    with tenant_session(ctx) as session:
        row = _resource_command(session, ctx, request, key, "marketing.ideas.photo", body.model_dump(mode="json"), add,
                                lambda s, resource: ideas.get_ideas_project(s, ctx, resource))
        set_etag(response, row.revision)
        return ideas.ideas_dict(session, ctx, row)


@router.post("/{project_id}/photos/{photo_id}/download-intent", operation_id="create_marketing_ideas_photo_download_intent",
             response_model=IdeasPhotoDownloadGrant, status_code=201, openapi_extra={"x-marketai-idempotent": True})
async def create_ideas_photo_download_intent(project_id: str, photo_id: str, request: Request,
                                             ctx: TenantContext = Depends(request_tenant_context),
                                             key: str = Depends(_idempotency_key)):
    """A short-lived signed link to one photo of these ideas (#233): its released derivative, the bytes an image
    request sends, never the uploaded original. Any photo listed in ``photos``, checked or not."""
    from datetime import timezone
    from app.platform.blob_store import DownloadSpec
    from app.platform.blob_store_factory import build_blob_store
    from app.platform.download_intents import DEFAULT_DOWNLOAD_TTL_SECONDS, create_or_replay_download_intent
    from app.platform.idempotency import compute_fingerprint
    from . import ideas_photos
    with tenant_session(ctx) as session:
        row = ideas.get_ideas_project(session, ctx, project_id)
        facts = ideas_photos.derivative(session, ctx, row, photo_id)
        intent = create_or_replay_download_intent(
            session, idempotency_key=key, command_type="marketing.ideas_photo_download_intent",
            fingerprint_hash=compute_fingerprint("POST", "create_marketing_ideas_photo_download_intent", request.url.path,
                                                 {}, None, {}),
            owner_kind="marketing_product_photo", owner_id=facts["reference_object_id"], committed_ref=facts["ref"])
        expires_at = intent.expires_at if intent.expires_at.tzinfo else intent.expires_at.replace(tzinfo=timezone.utc)
    remaining = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    grant = await build_blob_store().issue_download(facts["ref"], DownloadSpec(
        filename=f"photo-{photo_id[:12]}.jpg", expires_in_seconds=min(DEFAULT_DOWNLOAD_TTL_SECONDS, remaining)))
    return {"photo_id": photo_id, "download_url": grant.url, "expires_at": grant.expires_at.isoformat(),
            "mime_type": facts["mime_type"], "byte_size": facts["byte_size"], "width": facts["width"],
            "height": facts["height"]}


@router.post("/{project_id}/download-intent", operation_id="download_marketing_ideas", response_model=IdeasDownload,
             status_code=201, openapi_extra={"x-marketai-idempotent": True})
async def download_ideas(project_id: str, body: DownloadIdeas, request: Request,
                         ctx: TenantContext = Depends(request_tenant_context), key: str = Depends(_idempotency_key)):
    """Download exactly the ads named, each through its own short-lived signed link (#198).

    Every one must be ``exportable``; otherwise nothing is handed out and 409 ``ads_not_exportable`` lists
    each refused ad's status and reasons in ``params.ads``. The same key returns the same download."""
    from app.platform.blob_store import DownloadSpec
    from app.platform.blob_store_factory import build_blob_store
    from . import ideas_export
    with tenant_session(ctx) as session:
        row = ideas.get_ideas_project(session, ctx, project_id)
        # Checked again on a replay: a download is never re-issued for an ad that stopped being exportable.
        ideas_export.assert_exportable(session, ctx, row, body.asset_version_ids)
        record = _resource_command(session, ctx, request, key, "marketing.ideas.download", body.model_dump(mode="json"),
            lambda s: ideas_export.download_intent(s, ctx, row, body.asset_version_ids),
            lambda s, resource: ideas_export.download_record(s, ctx, row, resource))
        record_id, grants = record.id, ideas_export.download_grants(session, ctx, row, record)
    store, downloads = build_blob_store(), []
    for item in grants:
        grant = await store.issue_download(item["ref"], DownloadSpec(filename=item["filename"],
                                                                     expires_in_seconds=min(item["remaining"], 300)))
        downloads.append({"asset_version_id": item["asset_version_id"], "combination_id": item["combination_id"],
                          "filename": item["filename"], "intent_id": item["intent_id"], "expires_at": item["expires_at"],
                          "download_grant": {"url": grant.url, "kind": "signed_get", "headers": {},
                                             "content_disposition": grant.content_disposition}})
    return {"id": record_id, "project_id": project_id, "downloads": downloads}
