"""The ads-first first batch (#193, ADR 0018; design doc §5.1, §5.2, §8): one start, one confirmed maximum.

"Start" reads the product, drafts the understanding, drafts the ideas, makes their images and checks
each image (#197) without another click. The user confirms one number, the start button's maximum: the maxima of the steps plus
one extra planning call, kept for drafting the ideas again when the application refuses the first draft
(maintainer decision on #193). Every step is still an ordinary record -- the product intake, the
planning calls, one generation run per ad, one single-image check per image -- reserved and settled on
its own. The checks are admitted by the advance in the Worker too (maintainer decision on #197; the cost
contract §11 lists the steps).

``advance`` is the whole state machine. It looks at the current step and, when that step has finished,
admits the next one; otherwise it does nothing, so it may run any number of times. Before admitting a
paid step it checks that the maxima reserved so far plus this step's stay within the confirmed number.
The Worker runs it as the ``marketing.first_batch`` Job, which every step's success and failure exit
queues through ``notify``; ``resume`` runs it in the request after the user acted on a stop.

The batch stops and waits for the user (``needs_attention``) when the page lists several models, when
the ideas have fewer than 2 claims (no images then), when the next step would pass the confirmed number,
when the ideas were refused twice, when a step failed, or while a call's outcome is uncertain (an
uncertain call is never sent again on its own). A page that cannot be read ends the batch (``failed``):
the remedy is to start again with pasted text or screenshots.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, ProductIntakeRecord, ProductRecord, new_id, utcnow
from app.generation.models import RunItemRecord
from app.generation.selection import SelectionError
from app.platform.audit import append_event
from app.platform.jobs import enqueue_in_session
from app.platform.operations import OperationResultRef, OperationState, create_operation, transition_operation
from app.platform.runtime_role import WORKER_ROLE, runs_as
from . import ideas, service
from .models import MarketingFirstBatchRecord, MarketingProjectRecord

JOB_TYPE = "marketing.first_batch"
RESOURCE_TYPE = "marketing_first_batch"
ACTIVE = ("reading", "understanding", "ideas", "generating", "checking")
ENDED = ("done", "failed")
#: Images the price is quoted for: 3 claims x 2 ways of telling x 1 ad (``ideas.FIRST_BATCH``). Ideas
#: with 2 claims make 4, which the same confirmation covers.
ADS = 6
#: The extra planning call the confirmed total keeps for drafting the ideas again once (#193).
IDEAS_RETRIES = 1
#: The first batch's images: one per way of telling, portrait for the feed (design doc §12 leaves it to us).
IMAGE_FORMAT = {"aspect_ratio": "4:5", "image_size": "1K", "variants": 1}
SOURCE_LIMIT = 16000
QUOTE_VERSION = "first-batch-v1"
#: Run item states of an image still being made.
IMAGE_IN_FLIGHT = ("pending", "queued", "running", "qc_pending", "paused")
#: Stops the user can continue from; the others end the batch or wait for a reconciliation.
RESUMABLE = ("several_models", "too_few_claims", "over_budget", "ideas_refused", "step_failed")


def _money(value: Decimal) -> str:
    value = Decimal(value)
    return "0" if value == 0 else format(value.normalize(), "f")


def _reading_maximum() -> Decimal:
    """The product intake's maximum, as ``create_intake`` reserves it for a BYOK reading."""
    from app.products import intake_extraction as extraction
    from app.providers.llm_chat import quote_llm_chat
    from app.providers.llm_registry import model_spec
    return quote_llm_chat(execution_mode="byok", max_output_tokens=extraction.MAX_OUTPUT_TOKENS,
                          input_tokens=extraction.input_token_budget(model_spec("openai", None))).amount


def _image(mode: str) -> dict:
    if mode == "mock":
        return {"provider": "mock", "model": "mock-v1", "format": dict(IMAGE_FORMAT)}
    # The deployment's default OpenAI image model; its 1K price must be in MARKETAI_GENERATION_MAX_COSTS_JSON.
    return {"provider": "openai", "model": config.OPENAI_IMAGE_MODEL, "format": dict(IMAGE_FORMAT)}


def _image_unit(image: dict) -> Decimal:
    """One image's maximum; ``SelectionError`` when the deployment has no price for it."""
    from app.generation.selection import maximum_unit_cost
    from app.providers.router import get_provider
    if image["model"] not in get_provider(image["provider"]).models:
        raise SelectionError("model_unavailable", "该供应商不支持所选模型。")
    return maximum_unit_cost(image["provider"], image["model"], image["format"]["image_size"]) * image["format"]["variants"]


def _planning(session, ctx, mode: str) -> tuple[Decimal, str, str | None]:
    """One planning call's maximum, its quote hash, and why planning is unavailable (or ``None``)."""
    if mode == "mock":
        return Decimal("0"), "", None
    cap = service.planner_capability(session, ctx)
    if not cap["available"]:
        return Decimal("0"), cap["quote_hash"], cap["unavailable_reason"]
    return Decimal(cap["maximum_cost"]), cap["quote_hash"], None


def _check_unit(session, ctx, mode: str) -> tuple[Decimal, dict]:
    """One image check's maximum and who checks (``ai_qc.mode_capability``); its ``reason`` when none can run."""
    from . import ai_qc
    cap = ai_qc.mode_capability(session, ctx, mode)
    return (Decimal("0") if cap["reason"] else Decimal(cap["unit_maximum"])), cap


def quote(session, ctx, execution_mode: str = "byok") -> dict:
    """The start button's maximum: reading, understanding, ideas, one more ideas draft, the images and their checks.

    Step 1 runs on one OpenAI key (design D8): without an active OpenAI key -- for instance with a
    Gemini key only -- the quote says ``openai_key_required`` before anything is started."""
    from app.generation.credential_binding import resolve_generation_credential
    reason = None
    image = _image(execution_mode)
    if execution_mode == "byok" and not resolve_generation_credential(session, ctx, "openai").get("active"):
        reason = "openai_key_required"
    reading = Decimal("0") if execution_mode == "mock" else _reading_maximum()
    planning, planning_hash, planning_reason = _planning(session, ctx, execution_mode)
    reason = reason or planning_reason
    try:
        unit = _image_unit(image)
    except SelectionError as exc:
        unit, reason = Decimal("0"), reason or exc.code
    check, check_cap = _check_unit(session, ctx, execution_mode)
    reason = reason or check_cap["reason"]
    planned = (("reading", 1, reading), ("understanding", 1, planning), ("ideas", 1, planning),
               ("ideas_retry", IDEAS_RETRIES, planning), ("images", ADS, unit), ("checks", ADS, check))
    total = sum((unit_cost * quantity for _, quantity, unit_cost in planned), Decimal("0"))
    if reason is None and execution_mode == "byok":
        from app.generation.selection import authorize_paid, check_budget
        try:
            authorize_paid(session, ctx)
            check_budget(session, ctx, total)
        except SelectionError as exc:
            reason = exc.code
    quote_hash = service.digest([QUOTE_VERSION, execution_mode, str(reading), planning_hash, str(planning),
                                 image, str(unit), ADS, IDEAS_RETRIES,
                                 [check_cap["provider"], check_cap["model"], str(check)]])
    return {"available": reason is None, "unavailable_reason": reason, "execution_mode": execution_mode,
            "maximum_cost": _money(total) if reason is None else "0", "currency": "USD", "quote_hash": quote_hash,
            "image_provider": image["provider"], "image_model": image["model"],
            "steps": [{"step": step, "quantity": quantity, "unit_maximum": _money(unit_cost),
                       "maximum_cost": _money(unit_cost * quantity)} for step, quantity, unit_cost in planned]}


# --------------------------------------------------------------------------- start and read


def get(session, ctx, batch_id: str, *, lock: bool = False) -> MarketingFirstBatchRecord:
    statement = select(MarketingFirstBatchRecord).where(MarketingFirstBatchRecord.workspace_id == ctx.workspace_id,
                                                        MarketingFirstBatchRecord.id == batch_id)
    batch = session.scalar(statement.with_for_update() if lock else statement)
    if batch is None:
        raise service.MarketingError("first_batch_not_found", "找不到这次开始。", 404)
    return batch


def _intake_problem(problem) -> service.MarketingError:
    return service.MarketingError(problem.code, problem.message, problem.status, params=problem.params)


def start(session, ctx, *, url, upload_intent_ids, text, market, ad_language, strategy_language, execution_mode,
          quote_hash) -> MarketingFirstBatchRecord:
    """Start reading a product under the confirmed maximum, or return the same start of the same input.

    The same input (link, uploads and text) for the same market, language and mode within 24 hours is
    the same first batch -- a second click, or the same link pasted again -- unless that batch ended."""
    from app.products import intake_service
    service.ensure_active(session, ctx)
    try:
        inputs = intake_service.normalized_inputs(ctx, url=url, upload_intent_ids=upload_intent_ids, text_input=text)
        fingerprint, _ = intake_service.input_fingerprint(session, ctx, inputs)
    except intake_service.IntakeProblem as problem:
        raise _intake_problem(problem) from None
    batch_cls = MarketingFirstBatchRecord
    same = session.scalar(select(batch_cls).join(ProductIntakeRecord, (ProductIntakeRecord.id == batch_cls.intake_id)
                                                 & (ProductIntakeRecord.workspace_id == ctx.workspace_id))
        .where(batch_cls.workspace_id == ctx.workspace_id, ProductIntakeRecord.input_fingerprint == fingerprint,
               batch_cls.market == market.strip(), batch_cls.ad_language == ad_language,
               batch_cls.execution_mode == execution_mode, batch_cls.state != "failed",
               batch_cls.created_at >= utcnow() - intake_service.REPLAY_WINDOW)
        .order_by(batch_cls.created_at.desc(), batch_cls.id.desc()).limit(1))
    if same is not None:
        return same
    quoted = quote(session, ctx, execution_mode)
    if not quoted["available"]:
        raise service.MarketingError("first_batch_unavailable", "现在不能开始，请先处理提示的问题。", 409,
                                     params={"reason": quoted["unavailable_reason"]})
    if quote_hash != quoted["quote_hash"]:
        raise service.MarketingError("first_batch_quote_changed", "费用已变化，请刷新后确认新的最高费用。", 409)
    batch_id = new_id("mfb")
    try:
        intake, _ = intake_service.admit_intake(session, ctx, inputs, idempotency_key=f"first-batch:{batch_id}",
                                                execution_mode=execution_mode, reuse_recent=False)
    except intake_service.IntakeProblem as problem:
        raise _intake_problem(problem) from None
    reading = Decimal(intake.evidence_json["admission"]["quote"]["max_amount"])
    operation = create_operation(session, command_type="marketing.first_batch", initial_state=OperationState.RUNNING,
                                 reason="first batch started", result_ref=OperationResultRef(RESOURCE_TYPE, batch_id))
    batch = MarketingFirstBatchRecord(
        id=batch_id, workspace_id=ctx.workspace_id, intake_id=intake.id, market=market.strip(), ad_language=ad_language,
        strategy_language=strategy_language, execution_mode=execution_mode, state="reading",
        progress_json={"image": _image(execution_mode), "ideas_retry_used": False,
                       "steps": [{"step": "reading", "resource_id": intake.id, "maximum": _money(reading)}]},
        confirmed_max_cost=quoted["maximum_cost"], reserved_total=_money(reading), quote_hash=quote_hash,
        operation_id=operation.id, initiator_id=ctx.initiator_id)
    session.add(batch)
    session.flush()
    append_event(session, event_type="marketing.first_batch_started", resource_type=RESOURCE_TYPE, resource_id=batch.id,
                 details={"intake_id": intake.id, "execution_mode": execution_mode, "quote_hash": quote_hash,
                          "maximum_cost": quoted["maximum_cost"]})
    return batch


def summaries(session, ctx, project_ids) -> dict:
    """Each project's first batch as the overview reads it: whether it stopped and waits for the user."""
    rows = session.scalars(select(MarketingFirstBatchRecord).where(
        MarketingFirstBatchRecord.workspace_id == ctx.workspace_id,
        MarketingFirstBatchRecord.project_id.in_(list(project_ids)))).all() if project_ids else []
    return {row.project_id: {"id": row.id, "state": row.state, "needs_attention": row.state == "needs_attention",
                             "reason": (row.attention_json or {}).get("reason")} for row in rows}


def detail(session, ctx, batch) -> dict:
    """The ``FirstBatchDetail`` of a batch, with what continuing would cost when it waits for the user."""
    progress = batch.progress_json or {}
    project = session.scalar(select(MarketingProjectRecord).where(
        MarketingProjectRecord.workspace_id == ctx.workspace_id, MarketingProjectRecord.id == batch.project_id)) \
        if batch.project_id else None
    planned = len(project.ideas_json.get("combinations", [])) if project is not None and project.ideas_json else None
    offer = resume_offer(session, ctx, batch)
    attention = dict(batch.attention_json) if batch.attention_json else None
    if attention is not None:
        # Whether this kind of stop is continued on this batch; ``resume`` is what continuing costs right now.
        attention["resumable"] = batch.state == "needs_attention" and (
            attention["reason"] in RESUMABLE or offer is not None)
    return {"id": batch.id, "operation_id": batch.operation_id, "state": batch.state,
            "needs_attention": batch.state == "needs_attention", "attention": attention,
            "resume": offer, "intake_id": batch.intake_id, "product_id": batch.product_id,
            "project_id": batch.project_id, "market": batch.market, "ad_language": batch.ad_language,
            "strategy_language": batch.strategy_language, "execution_mode": batch.execution_mode,
            "selected_variant": batch.selected_variant, "confirmed_max_cost": batch.confirmed_max_cost,
            "reserved_total": batch.reserved_total, "currency": "USD",
            "image_provider": progress.get("image", {}).get("provider", ""),
            "image_model": progress.get("image", {}).get("model", ""), "ads_planned": planned,
            "created_at": _iso(batch.created_at), "updated_at": _iso(batch.updated_at)}


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


# --------------------------------------------------------------------------- notify and advance


def notify(session, ctx, *, intake_id: str | None = None, project_id: str | None = None, event: str) -> None:
    """Queue an advance of the first batch this intake or project belongs to, if any is still running.

    Called in the same transaction as a step's final state: the intake ready or failed, a planning call
    succeeded, failed or uncertain, an image made, failed or uncertain. The same event queues one Job."""
    if not intake_id and not project_id:
        return
    column = MarketingFirstBatchRecord.intake_id if intake_id else MarketingFirstBatchRecord.project_id
    batch = session.scalar(select(MarketingFirstBatchRecord).where(
        MarketingFirstBatchRecord.workspace_id == ctx.workspace_id, column == (intake_id or project_id)))
    if batch is None or batch.state in ENDED:
        return
    enqueue_in_session(session, context=ctx, job_type=JOB_TYPE, payload={"first_batch_id": batch.id},
                       idempotency_key=f"first_batch:{batch.id}:{event}"[:200], resource_type=RESOURCE_TYPE,
                       resource_id=batch.id)


@runs_as(WORKER_ROLE)
def run_advance(ctx, batch_id: str) -> None:
    """The ``marketing.first_batch`` Job: advance one batch under its row lock."""
    with tenant_session(ctx) as session:
        try:
            batch = get(session, ctx, batch_id, lock=True)
        except service.MarketingError:
            return
        advance(session, ctx, batch)


STEP_OF_STATE = {"reading": "reading", "understanding": "understanding", "ideas": "ideas", "generating": "images",
                 "checking": "checks"}


@runs_as(WORKER_ROLE)
def record_fault(ctx, batch_id: str) -> None:
    """The advance Job failed on its last attempt: stop, so the user can continue it (``resume``)."""
    with tenant_session(ctx) as session:
        try:
            batch = get(session, ctx, batch_id, lock=True)
        except service.MarketingError:
            return
        if batch.state in ACTIVE:
            _stop(session, ctx, batch, "step_failed", STEP_OF_STATE[batch.state], code="advance_failed")


def advance(session, ctx, batch, *, resumed: bool = False) -> None:
    """Admit the next step when the current one has finished; otherwise change nothing.

    ``resumed`` is the user's go-ahead after a stop: a failed step is admitted again instead of stopping."""
    if batch.state in ENDED:
        return
    if batch.state == "needs_attention" and not resumed and (batch.attention_json or {}).get("reason") != "uncertain":
        return
    if batch.project_id is None and not _read(session, ctx, batch):
        return
    try:
        row = ideas.get_ideas_project(session, ctx, batch.project_id)
    except service.MarketingError:
        _stop(session, ctx, batch, "step_failed", "understanding", code="ideas_unavailable", state="failed")
        return
    call = service.latest_call(session, ctx, row.id)
    if call is not None and call.state in ("queued", "running"):
        _set_state(batch, "understanding" if call.kind == "understanding" else "ideas")
        return
    if call is not None and call.state == "uncertain":
        _stop(session, ctx, batch, "uncertain", _step_of(call.kind), call_id=call.id)
        return
    if row.state == "input":
        if call is not None and call.kind == "understanding" and call.state == "failed" and not resumed:
            _stop(session, ctx, batch, "step_failed", "understanding", code=call.error_code)
            return
        _admit_planning(session, ctx, batch, row, "understanding")
        return
    if row.state == "understanding_draft":
        if call is not None and call.kind == "ideas" and call.state == "failed" and not resumed:
            if call.error_code != "planner_output_invalid":
                _stop(session, ctx, batch, "step_failed", "ideas", code=call.error_code)
            elif too_few_claims(call):
                _stop(session, ctx, batch, "too_few_claims", "ideas")
            elif not (batch.progress_json or {}).get("ideas_retry_used"):
                # The confirmed total kept one planning call for this: the next draft carries the refused
                # draft and its errors (``service.planning_repair_context``).
                _admit_planning(session, ctx, batch, row, "ideas", retry=True)
            else:
                _stop(session, ctx, batch, "ideas_refused", "ideas", code=call.error_code)
            return
        _admit_planning(session, ctx, batch, row, "ideas")
        return
    _generate(session, ctx, batch, row, resumed)


def _step_of(kind: str) -> str:
    return {"understanding": "understanding", "ideas": "ideas"}.get(kind, "ideas")


def too_few_claims(call) -> bool:
    """Whether a refused ideas draft had fewer than 2 claims (#193).

    The application refuses it like any invalid draft (``planner_output_invalid``); only the call's
    recorded validation errors tell it apart: the ``angles`` list itself is too short."""
    errors = (call.output_json or {}).get("validation_errors") or []
    return any(isinstance(error, dict) and list(error.get("loc") or []) == ["angles"] and error.get("type") == "too_short"
               for error in errors)


def _set_state(batch, state: str) -> None:
    if batch.state != state or batch.attention_json is not None:
        batch.state, batch.attention_json, batch.updated_at = state, None, utcnow()


def _stop(session, ctx, batch, reason: str, step: str, *, state: str = "needs_attention", **details) -> None:
    attention = {"reason": reason, "step": step, **{k: v for k, v in details.items() if v is not None}}
    if batch.state == state and batch.attention_json == attention:
        return
    batch.state, batch.attention_json, batch.updated_at = state, attention, utcnow()
    append_event(session, event_type="marketing.first_batch_stopped", resource_type=RESOURCE_TYPE, resource_id=batch.id,
                 details=attention)
    if state == "failed":
        transition_operation(session, batch.operation_id, OperationState.FAILED, f"first batch stopped: {reason}",
                             ctx.executor)


def _record_step(batch, step: str, maximum: Decimal, **extra) -> None:
    progress = dict(batch.progress_json or {})
    progress["steps"] = [*progress.get("steps", []), {"step": step, "maximum": _money(maximum), **extra}]
    batch.progress_json = progress
    batch.reserved_total = _money(Decimal(batch.reserved_total) + maximum)
    batch.updated_at = utcnow()


def _fits(batch, maximum: Decimal) -> bool:
    return Decimal(batch.reserved_total) + maximum <= Decimal(batch.confirmed_max_cost)


ADMISSION_ERRORS = (service.MarketingError, SelectionError)


def _admission_errors():
    from app.entitlements.reservation_book import GuardrailExceededError
    from app.entitlements.storage_quota import StorageAdmissionError
    from app.generation.preview import PreviewError
    from app.generation.submission import SubmissionError
    return (*ADMISSION_ERRORS, GuardrailExceededError, StorageAdmissionError, PreviewError, SubmissionError)


def _code(exc) -> str:
    return getattr(exc, "code", None) or ("budget_exceeded" if type(exc).__name__ == "GuardrailExceededError"
                                          else "admission_refused")


def _read(session, ctx, batch) -> bool:
    """Turn a finished reading into a product draft and its ideas; ``True`` once the project exists."""
    from app.products.intake_service import IN_FLIGHT_STATUSES
    intake = session.scalar(select(ProductIntakeRecord).where(ProductIntakeRecord.workspace_id == ctx.workspace_id,
                                                              ProductIntakeRecord.id == batch.intake_id))
    if intake.status in IN_FLIGHT_STATUSES:
        _set_state(batch, "reading")
        return False
    if intake.status == "fetch_failed":  # the page could not be read, or is not a product page
        _stop(session, ctx, batch, "page_unreadable", "reading", code=intake.failure_code, state="failed")
        return False
    if intake.status not in ("ready", "confirmed"):
        _stop(session, ctx, batch, "reading_failed", "reading", code=intake.failure_code or intake.status, state="failed")
        return False
    variants = [v["label"] for v in (intake.card_json or {}).get("model_variants") or []
                if isinstance(v, dict) and v.get("label")]
    if len(variants) > 1 and batch.selected_variant not in variants:
        _stop(session, ctx, batch, "several_models", "reading", variants=variants[:20])
        return False
    from app.library.service import LibraryError
    from app.products.intake_service import IntakeProblem
    try:
        with session.begin_nested():
            product_id = intake.product_id if intake.status == "confirmed" and intake.product_id \
                else _product_draft(session, ctx, batch, intake)
            row = ideas.create_ideas_project(session, ctx, product_id=product_id,
                                             source_text=source_text(intake, batch.selected_variant),
                                             market=batch.market, ad_language=batch.ad_language)
    except (IntakeProblem, LibraryError, service.MarketingError) as exc:  # a card that cannot become a product
        _stop(session, ctx, batch, "reading_failed", "reading", code=getattr(exc, "code", "reading_failed"), state="failed")
        return False
    batch.product_id, batch.project_id = product_id, row.id
    _set_state(batch, "understanding")
    append_event(session, event_type="marketing.first_batch_read", resource_type=RESOURCE_TYPE, resource_id=batch.id,
                 details={"product_id": product_id, "project_id": row.id, "selected_variant": batch.selected_variant})
    return True


def _product_draft(session, ctx, batch, intake) -> str:
    """The reading as a creative-library product draft, not published (design D3); its ``prod_`` anchor.

    What the AI read stays a proposal: nothing is marked confirmed, and no photo is bound yet (S11)."""
    from app.library import service as library
    from app.library.contracts import ENTRY_PRODUCT
    from app.products.intake_service import proposed_content
    content = proposed_content(intake, batch.selected_variant)
    entry = library.create_entry_in_transaction(session, ctx, entry_type=ENTRY_PRODUCT,
                                                display_name=content.get("name") or "未命名产品", content=content)
    return session.scalar(select(ProductRecord.id).where(ProductRecord.workspace_id == ctx.workspace_id,
                                                        ProductRecord.product_profile_entry_id == entry.id))


def source_text(intake, selected_variant: str | None = None) -> str:
    """The ideas' source (design D4): the product page's text summary and what the user pasted.

    With images only, the text the model read from them; the chosen model is named last. The user's own
    text is kept whole before the page summary is shortened to fit."""
    from app.products.intake_service import proposed_content
    page = (((intake.evidence_json or {}).get("fetch") or {}).get("digest_text") or "").strip()
    pasted = (intake.input_text or "").strip()
    tail = f"\n\nChosen model: {selected_variant}" if selected_variant else ""
    if not page and not pasted:
        page = service.card_text(proposed_content(intake, selected_variant))
    room = SOURCE_LIMIT - len(tail)
    pasted = pasted[:room]
    page = page[:max(0, room - len(pasted) - 2)].strip() if pasted else page[:room].strip()
    return "\n\n".join(part for part in (page, pasted) if part) + tail


def _admit_planning(session, ctx, batch, row, kind: str, *, retry: bool = False) -> None:
    step = kind
    maximum, quote_hash, unavailable = _planning(session, ctx, batch.execution_mode)
    if unavailable:
        _stop(session, ctx, batch, "step_failed", step, code=unavailable)
        return
    if not _fits(batch, maximum):
        _stop(session, ctx, batch, "over_budget", step, needed=_money(Decimal(batch.reserved_total) + maximum))
        return
    try:
        with session.begin_nested():
            call = ideas.start_ideas_planning(session, ctx, row, row.revision, kind, batch.execution_mode, quote_hash,
                                              batch.strategy_language)
    except _admission_errors() as exc:
        _stop(session, ctx, batch, "step_failed", step, code=_code(exc))
        return
    if retry:
        batch.progress_json = {**(batch.progress_json or {}), "ideas_retry_used": True}
    _record_step(batch, step, Decimal(call.maximum_cost), resource_id=call.id, **({"retry": True} if retry else {}))
    _set_state(batch, "understanding" if kind == "understanding" else "ideas")


def _image_runs(batch) -> dict:
    """The newest run admitted for each ad (combination id) of this batch."""
    runs = {}
    for step in (batch.progress_json or {}).get("steps", []):
        if step.get("step") == "images":
            runs.update(step.get("runs") or {})
    return runs


def image_states(session, ctx, batch) -> dict:
    """Each admitted ad's state: ``made``, ``in_flight``, ``uncertain`` or ``failed``."""
    runs = _image_runs(batch)
    if not runs:
        return {}
    items = session.scalars(select(RunItemRecord).where(RunItemRecord.workspace_id == ctx.workspace_id,
                                                        RunItemRecord.generation_run_id.in_(list(runs.values())))).all()
    made = set(session.scalars(select(AssetVersionRecord.producing_run_item_id).where(
        AssetVersionRecord.workspace_id == ctx.workspace_id,
        AssetVersionRecord.producing_run_item_id.in_([item.id for item in items]))).all()) if items else set()
    by_run = {}
    for item in items:
        by_run.setdefault(item.generation_run_id, []).append(item)
    states = {}
    for combination_id, run_id in runs.items():
        run_items = by_run.get(run_id, [])
        if run_items and all(item.id in made or item.state in ("succeeded", "reused") for item in run_items):
            states[combination_id] = "made"
        elif any(item.state in IMAGE_IN_FLIGHT and item.id not in made for item in run_items):
            states[combination_id] = "in_flight"
        elif any(item.state == "uncertain" for item in run_items):
            states[combination_id] = "uncertain"
        else:
            states[combination_id] = "failed"
    return states


def _generate(session, ctx, batch, row, resumed: bool) -> None:
    combinations = [c["id"] for c in (row.ideas_json or {}).get("combinations", [])]
    states = image_states(session, ctx, batch)
    if any(states.get(c) == "in_flight" for c in combinations):
        _set_state(batch, "generating")
        return
    uncertain = [c for c in combinations if states.get(c) == "uncertain"]
    if uncertain:
        _stop(session, ctx, batch, "uncertain", "images", combination_ids=uncertain)
        return
    failed = [c for c in combinations if states.get(c) == "failed"]
    if failed and not resumed:
        _stop(session, ctx, batch, "step_failed", "images", combination_ids=failed)
        return
    todo = [c for c in combinations if c not in states or c in failed]
    if not todo:
        _check(session, ctx, batch, row, resumed)
        return
    _admit_images(session, ctx, batch, row, todo)


def _admit_images(session, ctx, batch, row, combination_ids) -> None:
    image = (batch.progress_json or {})["image"]
    try:
        unit = _image_unit(image)
    except SelectionError as exc:
        _stop(session, ctx, batch, "step_failed", "images", code=exc.code)
        return
    maximum = unit * len(combination_ids)
    if not _fits(batch, maximum):
        _stop(session, ctx, batch, "over_budget", "images", needed=_money(Decimal(batch.reserved_total) + maximum))
        return
    from app.generation import preview as preview_module, submission as submission_module
    from app.generation.draft import get_draft
    try:
        with session.begin_nested():
            drafts = {d["combination_id"]: d["draft_id"] for d in ideas.generation_drafts(
                session, ctx, row, row.revision, image["provider"], image["model"], image["format"])}
            runs = {}
            for combination_id in combination_ids:
                draft = get_draft(session, ctx, drafts[combination_id])
                preview = preview_module.create_preview(session, ctx, draft.id, draft.revision)
                submitted = submission_module.submit_generation(
                    session, ctx, preview_id=preview.id, preview_hash=preview.preview_hash,
                    expected_draft_revision=draft.revision, idempotency_key=f"first-batch:{batch.id}:{preview.id}")
                runs[combination_id] = submitted["generation_run_id"]
    except _admission_errors() as exc:
        _stop(session, ctx, batch, "step_failed", "images", code=_code(exc))
        return
    _record_step(batch, "images", maximum, runs=runs)
    _set_state(batch, "generating")


def batch_images(session, ctx, batch) -> dict:
    """The images this batch's runs made: asset version id -> its ad (combination id), oldest first."""
    runs = _image_runs(batch)
    if not runs:
        return {}
    ad_of = {run_id: combination_id for combination_id, run_id in runs.items()}
    rows = session.execute(select(AssetVersionRecord.id, RunItemRecord.generation_run_id)
        .join(RunItemRecord, (RunItemRecord.id == AssetVersionRecord.producing_run_item_id)
              & (RunItemRecord.workspace_id == ctx.workspace_id))
        .where(AssetVersionRecord.workspace_id == ctx.workspace_id, RunItemRecord.generation_run_id.in_(list(runs.values())))
        .order_by(AssetVersionRecord.created_at, AssetVersionRecord.id)).all()
    return {asset_id: ad_of[run_id] for asset_id, run_id in rows}


def check_states(session, ctx, batch) -> dict:
    """The state of each batch image's newest check, whoever started it: absent while it has none."""
    from .ideas_checks import latest_by_asset
    images = batch_images(session, ctx, batch)
    latest = latest_by_asset(session, ctx, batch.project_id) if images else {}
    return {asset_id: latest[asset_id] for asset_id in images if asset_id in latest}


def _check(session, ctx, batch, row, resumed: bool) -> None:
    """Every image is made: check each one (maintainer decision on #197), then the batch is done.

    The images are already on the ideas page while they are being checked. A check that failed stops the
    batch until the user continues; an uncertain one waits for its reconciliation and is never resent."""
    images = batch_images(session, ctx, batch)
    calls = check_states(session, ctx, batch)
    states = {asset_id: call.state for asset_id, call in calls.items()}
    if any(state in ("queued", "running") for state in states.values()):
        _set_state(batch, "checking")
        return
    uncertain = [images[a] for a in images if states.get(a) == "uncertain"]
    if uncertain:
        _stop(session, ctx, batch, "uncertain", "checks", combination_ids=uncertain)
        return
    failed = [a for a in images if states.get(a) == "failed"]
    if failed and not resumed:
        _stop(session, ctx, batch, "step_failed", "checks", code=calls[failed[0]].error_code,
              combination_ids=[images[a] for a in failed])
        return
    todo = [a for a in images if a not in states or a in failed]
    if not todo:
        _done(session, ctx, batch)
        return
    _admit_checks(session, ctx, batch, row, {a: images[a] for a in todo})


def _admit_checks(session, ctx, batch, row, todo: dict) -> None:
    from . import ideas_checks
    unit, cap = _check_unit(session, ctx, batch.execution_mode)
    if cap["reason"]:
        _stop(session, ctx, batch, "step_failed", "checks", code=cap["reason"])
        return
    maximum = unit * len(todo)
    if not _fits(batch, maximum):
        _stop(session, ctx, batch, "over_budget", "checks", needed=_money(Decimal(batch.reserved_total) + maximum),
              combination_ids=list(todo.values()))
        return
    try:
        with session.begin_nested():
            records = ideas_checks.admit_for_first_batch(session, ctx, row, batch.execution_mode, list(todo), batch.id)
    except _admission_errors() as exc:
        _stop(session, ctx, batch, "step_failed", "checks", code=_code(exc))
        return
    if records:
        _record_step(batch, "checks", sum((Decimal(r.maximum_cost) for r in records), Decimal("0")),
                     calls={r.asset_version_id: r.id for r in records})
    _set_state(batch, "checking")


def _done(session, ctx, batch) -> None:
    batch.state, batch.attention_json, batch.updated_at = "done", None, utcnow()
    transition_operation(session, batch.operation_id, OperationState.SUCCEEDED, "first batch done", ctx.executor)
    append_event(session, event_type="marketing.first_batch_done", resource_type=RESOURCE_TYPE, resource_id=batch.id,
                 details={"project_id": batch.project_id, "reserved_total": batch.reserved_total,
                          "confirmed_max_cost": batch.confirmed_max_cost})


# --------------------------------------------------------------------------- resume


def _still_uncertain(session, ctx, batch) -> bool:
    attention = batch.attention_json or {}
    if attention.get("step") == "checks":
        return any(call.state == "uncertain" for call in check_states(session, ctx, batch).values())
    if attention.get("step") == "images":
        return any(state == "uncertain" for state in image_states(session, ctx, batch).values())
    if batch.project_id is None:
        return False
    call = service.latest_call(session, ctx, batch.project_id)
    return call is not None and call.state == "uncertain"


def resume_offer(session, ctx, batch) -> dict | None:
    """What continuing from this stop needs and costs at most, or ``None`` when it cannot continue now.

    The maximum is the whole batch's new confirmed number: what was reserved so far plus the rest of
    the steps from where it stopped. Continuing confirms it, like the start button."""
    attention = batch.attention_json or {}
    reason, step = attention.get("reason"), attention.get("step")
    if batch.state != "needs_attention":
        return None
    if reason == "uncertain":
        if _still_uncertain(session, ctx, batch):
            return None  # reconcile the call first; it is never sent again on its own
    elif reason not in RESUMABLE:
        return None
    planning, planning_hash, unavailable = _planning(session, ctx, batch.execution_mode)
    try:
        unit = _image_unit((batch.progress_json or {})["image"])
    except SelectionError:
        return None
    check, check_cap = _check_unit(session, ctx, batch.execution_mode)
    if unavailable or check_cap["reason"]:
        return None
    retries = 0 if (batch.progress_json or {}).get("ideas_retry_used") else IDEAS_RETRIES
    concerned = len(attention.get("combination_ids") or []) or ADS
    if step == "checks":
        remaining = check * concerned
    elif step == "images":
        remaining = unit * concerned + check * ADS
    elif reason == "ideas_refused":
        remaining = planning + (unit + check) * ADS
    elif step == "ideas" and reason != "too_few_claims":
        remaining = planning * (1 + retries) + (unit + check) * ADS
    else:  # several models, too few claims, or the understanding: the understanding and everything after it
        remaining = planning * (2 + retries) + (unit + check) * ADS
    maximum = max(Decimal(batch.reserved_total) + remaining, Decimal(batch.confirmed_max_cost))
    requires = (["selected_variant"] if reason == "several_models" else []) + (
        ["additional_text"] if reason == "too_few_claims" else [])
    quote_hash = service.digest([QUOTE_VERSION, "resume", batch.id, attention, batch.reserved_total,
                                 batch.confirmed_max_cost, str(maximum), planning_hash, str(unit), str(check)])
    return {"maximum_cost": _money(maximum), "additional_cost": _money(maximum - Decimal(batch.confirmed_max_cost)),
            "currency": "USD", "quote_hash": quote_hash, "requires": requires}


RESUME_STATE = {"reading": "reading", "understanding": "understanding", "ideas": "ideas", "images": "generating",
                "checks": "checking"}


def resume(session, ctx, batch, *, quote_hash: str, selected_variant: str | None = None,
           additional_text: str | None = None) -> MarketingFirstBatchRecord:
    """Continue a stopped batch after the user acted on it, confirming the offered maximum."""
    service.ensure_active(session, ctx)
    if batch.state != "needs_attention":
        raise service.MarketingError("first_batch_not_stopped", "这次开始没有停下来，不需要继续。", 409)
    offer = resume_offer(session, ctx, batch)
    if offer is None:
        raise service.MarketingError("first_batch_not_resumable", "现在还不能继续，请先处理提示的问题。", 409)
    if quote_hash != offer["quote_hash"]:
        raise service.MarketingError("first_batch_quote_changed", "费用已变化，请刷新后确认新的最高费用。", 409)
    attention = dict(batch.attention_json)
    if "selected_variant" in offer["requires"]:
        if selected_variant not in (attention.get("variants") or []):
            raise service.MarketingError("variant_selection_required", "请选择这个链接里的一个型号。",
                                         fields=["body.selected_variant"])
        batch.selected_variant = selected_variant
    if "additional_text" in offer["requires"]:
        if not (additional_text or "").strip():
            raise service.MarketingError("source_required", "请补充产品资料后再继续。", fields=["body.additional_text"])
        row = ideas.get_ideas_project(session, ctx, batch.project_id)
        supplemented = f"{row.source_text}\n\n{additional_text.strip()}"
        if len(supplemented) > SOURCE_LIMIT:
            raise service.MarketingError("source_invalid", "请粘贴不超过 16000 字的产品资料。",
                                         params={"limit": SOURCE_LIMIT}, fields=["body.additional_text"])
        # More material means a new understanding; the refused ideas went with the old one.
        service.advance(session, ctx, row, row.revision, "source_supplemented", state="input", source_text=supplemented,
                        understanding_json=None, ideas_json=None)
    batch.confirmed_max_cost = offer["maximum_cost"]
    batch.state, batch.attention_json, batch.updated_at = RESUME_STATE[attention["step"]], None, utcnow()
    append_event(session, event_type="marketing.first_batch_resumed", resource_type=RESOURCE_TYPE, resource_id=batch.id,
                 details={"reason": attention["reason"], "step": attention["step"], "maximum_cost": offer["maximum_cost"],
                          "quote_hash": quote_hash})
    session.flush()
    advance(session, ctx, batch, resumed=True)
    return batch
