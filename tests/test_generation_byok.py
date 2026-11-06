"""BYOK Generation: OpenAI GPT-Image through the Run chain.

Covers the pieces that turn a ``byok`` Draft into real images:

* the price table and the preview quote it feeds;
* capabilities advertising ``openai`` and ``byok`` only once a key is bound;
* admission-time spend reservation (one per RunItem, whole-command guardrail);
* the Worker's coordinated dispatch -- success lands a committed AssetVersion
  and settles the reservation, a terminal refusal fails the item and
  releases it, an unknown outcome parks the item as uncertain, a missing
  key pauses it, and Retry reserves again;
* the OpenAI adapter's HTTP contract against a mocked transport;
* the credential admin commands on column-level reads.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import unittest
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    JobRecord,
    new_id,
)
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendGuardrailRecord,
    SpendReservationRecord,
)
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import LocalKmsAdapter
from app.entitlements.models import EntitlementProjectionRecord
from app.generation import pricing
from app.generation.controlled_adapter import GenerationOutput
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.openai_adapter import ByokGenerationRequest, OpenAIImageAdapter
from app.generation.spend import reservation_semantic_key
from app.generation.vlm_qc import QC_CALL_NAME, qc_max_cost
from app.platform.blob_store import CommittedBlobRef, GeneratedArtifactSpec
from app.providers.adapter import ProviderCapabilities, ProviderDispatchError, ProviderResult
from app.providers.dispatch import ExternalCallCoordinator
from app.providers.llm_chat import llm_call_key

#: Issue #169: every byok image is quality-checked by GPT-5 mini (no
#: references bound in these tests: the generated image only).
QC = qc_max_cost("openai", "gpt-5-mini", 1)
from tests.support.library_fixtures import LibraryFixtures

PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class TestPricing(unittest.TestCase):
    def test_unit_price_follows_model_quality_and_size(self):
        self.assertEqual(
            pricing.unit_price("openai", "gpt-image-2", {"aspect_ratio": "1:1"}),
            Decimal("0.053"),
        )
        self.assertEqual(
            pricing.unit_price("openai", "gpt-image-2", {"aspect_ratio": "9:16", "quality": "high"}),
            Decimal("0.165"),
        )
        self.assertEqual(
            pricing.unit_price("openai", "gpt-image-1-mini", {"aspect_ratio": "16:9", "quality": "low"}),
            Decimal("0.006"),
        )

    def test_format_resolution(self):
        self.assertEqual(pricing.openai_size({"aspect_ratio": "4:5"}), "1024x1536")
        self.assertEqual(pricing.openai_size({"aspect_ratio": "16:9"}), "1536x1024")
        self.assertEqual(pricing.openai_size({"resolution": "1536x1024"}), "1536x1024")
        self.assertEqual(pricing.openai_size({}), "1024x1024")
        self.assertEqual(pricing.openai_quality({"quality": "HIGH"}), "high")
        self.assertEqual(pricing.openai_quality({"quality": "ultra"}), "medium")

    def test_unknown_provider_or_model_is_unpriced(self):
        with self.assertRaises(pricing.UnpricedCallError):
            pricing.unit_price("meta", "llama-image-v1", {})
        with self.assertRaises(pricing.UnpricedCallError):
            pricing.unit_price("openai", "dall-e-2", {})


# ---------------------------------------------------------------------------
# Adapter HTTP contract
# ---------------------------------------------------------------------------


def _request() -> ByokGenerationRequest:
    from app.generation.creative_recipe import CreativeRecipe, OutputFormat, ProductBrief

    return ByokGenerationRequest(
        recipe=CreativeRecipe(product=ProductBrief(name="a charger"), format=OutputFormat(aspect_ratio="1:1")),
        model="gpt-image-2",
        semantic_fingerprint="f" * 64,
    )


def _adapter(handler) -> OpenAIImageAdapter:
    return OpenAIImageAdapter(
        base_url="https://openai.test/v1", timeout=5, transport=httpx.MockTransport(handler),
    )


class TestOpenAIImageAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_decoded_bytes_and_request_id(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["Authorization"]
            seen["body"] = json.loads(request.content)
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                headers={"x-request-id": "req_123"},
                json={"data": [{"b64_json": base64.b64encode(PNG).decode()}], "usage": {"output_tokens": 7}},
            )

        result = await _adapter(handler).execute(
            _request(), credential=memoryview(b"sk-test-key"), idempotency_key="k",
        )
        self.assertEqual(seen["auth"], "Bearer sk-test-key")
        self.assertEqual(seen["url"], "https://openai.test/v1/images/generations")
        self.assertEqual(seen["body"]["model"], "gpt-image-2")
        self.assertEqual(seen["body"]["size"], "1024x1024")
        self.assertEqual(seen["body"]["quality"], "medium")
        self.assertEqual(result.provider_request_id, "req_123")
        self.assertEqual(result.output.content_bytes, PNG)
        self.assertEqual(result.output.content_sha256, hashlib.sha256(PNG).hexdigest())
        self.assertEqual(result.output.mime_type, "image/png")
        self.assertEqual(result.output.usage, {"output_tokens": 7})

    async def test_provider_refusals_are_terminal(self):
        for status, code in ((401, "credential_rejected"), (429, "rate_limited"), (400, "openai_400")):
            with self.subTest(status=status):
                def handler(request: httpx.Request, status=status) -> httpx.Response:
                    return httpx.Response(status, json={"error": {"message": "nope"}})

                with self.assertRaises(ProviderDispatchError) as raised:
                    await _adapter(handler).execute(
                        _request(), credential=memoryview(b"k"), idempotency_key="k",
                    )
                self.assertEqual(raised.exception.dispatch_certainty, "not_dispatched")
                self.assertEqual(raised.exception.error_code, code)
                self.assertIn("nope", str(raised.exception))

    async def test_server_errors_and_timeouts_are_uncertain(self):
        def server_error(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="bad gateway")

        with self.assertRaises(ProviderDispatchError) as raised:
            await _adapter(server_error).execute(
                _request(), credential=memoryview(b"k"), idempotency_key="k",
            )
        self.assertEqual(raised.exception.dispatch_certainty, "uncertain")

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with self.assertRaises(ProviderDispatchError) as raised:
            await _adapter(timeout).execute(
                _request(), credential=memoryview(b"k"), idempotency_key="k",
            )
        self.assertEqual(raised.exception.dispatch_certainty, "uncertain")
        self.assertEqual(raised.exception.error_code, "provider_timeout")

    async def test_missing_credential_never_calls_the_provider(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"data": []})

        with self.assertRaises(ProviderDispatchError) as raised:
            await _adapter(handler).execute(_request(), credential=None, idempotency_key="k")
        self.assertEqual(raised.exception.error_code, "credential_unavailable")
        self.assertEqual(calls, [])

    def test_capabilities_make_reconcile_conservative(self):
        capabilities = OpenAIImageAdapter(transport=httpx.MockTransport(lambda r: httpx.Response(500))).capabilities
        self.assertFalse(capabilities.accepts_idempotency_key)
        self.assertFalse(capabilities.supports_result_lookup_by_request_id)


# ---------------------------------------------------------------------------
# Run chain (admission + Worker)
# ---------------------------------------------------------------------------


class _RecordingBlobStore:
    """Commits generated artifacts to memory, like the bucket adapter would."""

    def __init__(self) -> None:
        self.committed: list[tuple[GeneratedArtifactSpec, bytes]] = []

    async def commit_generated_artifact(self, source: Path, spec: GeneratedArtifactSpec) -> CommittedBlobRef:
        content = source.read_bytes()
        self.committed.append((spec, content))
        return CommittedBlobRef(
            committed_key=spec.committed_key,
            storage_version_id=f"v{len(self.committed)}",
            size_bytes=len(content),
            checksum_sha256_b64=spec.expected_sha256_b64,
            content_type_verified=spec.content_type,
        )


class _ScriptedAdapter:
    """A real-provider stand-in whose outcomes the test scripts in order."""

    provider = "openai"
    capabilities = ProviderCapabilities(
        accepts_idempotency_key=False, supports_result_lookup_by_request_id=False,
    )

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[tuple[ByokGenerationRequest, bytes]] = []

    async def execute(self, request, *, credential, idempotency_key):
        self.requests.append((request, bytes(credential) if credential is not None else b""))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        digest = hashlib.sha256(outcome).hexdigest()
        return ProviderResult(
            output=GenerationOutput(
                object_key=f"generation/openai/{digest}.png",
                content_sha256=digest,
                byte_size=len(outcome),
                mime_type="image/png",
                usage={"output_tokens": 1},
                content_bytes=outcome,
            ),
            provider_request_id="req_scripted",
            idempotency_key_accepted=False,
        )


class ByokRunChainTests(LibraryFixtures):
    """Draft -> Preview -> Submit -> Worker for ``openai`` / ``byok``."""

    PROTECTOR = LocalKmsAdapter.local_only()

    def setUp(self) -> None:
        self.ctx = self._make_context(role="owner", actor="owner_a")
        self.ctx = self.owner_ctx(self.ctx)
        with tenant_session(self.ctx) as s:
            s.add(EntitlementProjectionRecord(
                workspace_id=self.ctx.workspace_id,
                entitlement_version=1, lease_sequence=1, projection_version=1,
                semantic_hash="hash_byok", resolved_entitlements_json={"features": ["generation"]},
            ))
            self.product_id = self._product_id(self.ctx, s)
        self.blob_store = _RecordingBlobStore()
        self.coordinator = ExternalCallCoordinator(session_factory=tenant_session)

    # -- helpers ----------------------------------------------------------

    def _bind_openai_key(self, plaintext: bytes = b"sk-live-test") -> str:
        admin = CredentialAdmin(self.PROTECTOR)
        with tenant_session(self.ctx) as s:
            meta = admin.create(
                s, self.ctx, credential_key="OPENAI_API_KEY", provider="openai", plaintext=plaintext,
            )
            admin.bind(
                s, self.ctx, binding_scope="generation", provider="openai",
                credential_version_id=meta.credential_version_id,
            )
            return meta.credential_version_id

    def _draft(self, s, *, variants: int = 2, aspect_ratio: str = "1:1"):
        from app.generation.draft import create_draft

        return create_draft(
            s, self.ctx,
            product_id=self.product_id,
            provider="openai", model="gpt-image-2", execution_mode="byok",
            combination_strategy={"kind": "full", "planner_version": "v1"},
            format_config={"aspect_ratio": aspect_ratio, "variants_per_combination": variants},
        )

    def _submit(self, s, *, variants: int = 2) -> tuple[str, str]:
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation

        draft = self._draft(s, variants=variants)
        preview = create_preview(s, self.ctx, draft.id, draft.revision)
        result = submit_generation(
            s, self.ctx,
            preview_id=preview.id, preview_hash=preview.preview_hash,
            expected_draft_revision=draft.revision, idempotency_key=new_id("ik"),
        )
        return result["generation_run_id"], result["operation_id"]

    def _items(self, s, run_id: str) -> list[RunItemRecord]:
        return s.execute(
            select(RunItemRecord)
            .where(RunItemRecord.generation_run_id == run_id)
            .order_by(RunItemRecord.ordinal)
        ).scalars().all()

    def _reservations(self, s, item_id: str, *, quality_check: bool = False) -> list[SpendReservationRecord]:
        """The image call's reservations per Job (or the quality check's, #169)."""
        item = s.get(RunItemRecord, item_id)
        job_ids = s.execute(select(JobRecord.id).where(
            JobRecord.resource_type == "run_item", JobRecord.resource_id == item_id,
        )).scalars().all()
        keys = [
            llm_call_key(job_id, QC_CALL_NAME) if quality_check
            else reservation_semantic_key(item.semantic_fingerprint, job_id)
            for job_id in job_ids
        ]
        return s.execute(
            select(SpendReservationRecord)
            .where(SpendReservationRecord.semantic_key_hash.in_(keys))
            .order_by(SpendReservationRecord.created_at, SpendReservationRecord.id)
        ).scalars().all()

    def _dispatch(self, adapter, item_id: str):
        from app.generation.byok_dispatch import handle_run_item_byok

        return asyncio.run(handle_run_item_byok(
            self.coordinator, self.ctx, item_id,
            adapters={"openai": adapter},
            blob_store=self.blob_store,
            protector=self.PROTECTOR,
            session_factory=tenant_session,
        ))

    # -- capabilities & preview --------------------------------------------

    def test_capabilities_advertise_openai_and_byok_only_with_a_bound_key(self):
        from app.generation.capabilities import get_capabilities

        with tenant_session(self.ctx) as s:
            before = get_capabilities(s, self.ctx)
        self.assertIn("openai", before["providers"])
        self.assertIn("gpt-image-2", before["models"]["openai"])
        self.assertNotIn("byok", before["modes"])

        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            after = get_capabilities(s, self.ctx)
        self.assertIn("byok", after["modes"])
        self.assertTrue(after["credential_readiness"]["openai"]["has_active_credential"])

    def test_preview_needs_a_bound_key_and_quotes_the_list_price(self):
        from app.generation.preview import PreviewError, create_preview

        with tenant_session(self.ctx) as s:
            draft = self._draft(s)
            with self.assertRaises(PreviewError) as raised:
                create_preview(s, self.ctx, draft.id, draft.revision)
            self.assertEqual(raised.exception.code, "credential_required")

        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            draft = self._draft(s, variants=2, aspect_ratio="16:9")
            preview = create_preview(s, self.ctx, draft.id, draft.revision)
            projections = preview.snapshot_json["ui_projections"]
            self.assertEqual(projections["call_count"], 2)
            # gpt-image-2, medium, 1536x1024 -> 0.041 per call, plus each
            # image's quality check (Issue #169).
            self.assertEqual(
                projections["maximum_cost"],
                {"amount": format(((Decimal("0.041") + QC) * 2).normalize(), "f"), "currency": "USD"},
            )
            decision = [d for d in projections["capability_decisions"] if d["capability"] == "quality_check"]
            self.assertTrue(decision[0]["supported"])
            recipe = preview.snapshot_json["recipe"]
            self.assertEqual(recipe["cost_policy"]["estimated_per_call"], "0.041")
            self.assertEqual(recipe["cost_policy"]["price_rule_version"], pricing.PRICE_RULE_VERSION)

    # -- admission ---------------------------------------------------------

    def test_submission_reserves_one_call_per_item_under_a_fresh_guardrail(self):
        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            run_id, operation_id = self._submit(s, variants=2)
            items = self._items(s, run_id)
            self.assertEqual(len(items), 2)
            run = s.get(GenerationRunRecord, run_id)
            self.assertEqual(run.execution_mode, "byok")
            self.assertEqual(run.est_cost, (Decimal("0.053") + QC) * 2)
            from app.generation.models import ResolutionPreviewConfirmationRecord

            confirmation = s.get(ResolutionPreviewConfirmationRecord, run.creation_preview_confirmation_id)
            self.assertEqual(confirmation.quoted_max_budget_amount, (Decimal("0.053") + QC) * 2)
            self.assertEqual(confirmation.quoted_currency, "USD")
            guardrail = s.get(SpendGuardrailRecord, self.ctx.workspace_id)
            self.assertIsNotNone(guardrail)
            self.assertEqual(guardrail.per_operation_limit, Decimal("5"))
            for item in items:
                reservations = self._reservations(s, item.id)
                self.assertEqual(len(reservations), 1)
                reservation = reservations[0]
                self.assertEqual(reservation.state, "reserved")
                self.assertEqual(reservation.operation_id, operation_id)
                self.assertEqual(reservation.maximum_provider_amount, Decimal("0.053"))
                self.assertEqual(reservation.execution_mode, "byok")
                job = s.get(JobRecord, reservation.job_id)
                self.assertEqual(job.resource_id, item.id)
                # The image's quality check is reserved by the same command.
                (check,) = self._reservations(s, item.id, quality_check=True)
                self.assertEqual(
                    (check.state, check.provider, check.maximum_provider_amount, check.job_id),
                    ("reserved", "openai", QC, reservation.job_id),
                )
            ledger_kinds = sorted(
                s.execute(
                    select(ProviderCostLedgerEntryRecord.entry_type).where(
                        ProviderCostLedgerEntryRecord.operation_id == operation_id
                    )
                ).scalars().all()
            )
            self.assertEqual(ledger_kinds, ["quote"] * 4 + ["reservation"] * 4)

    def test_admission_writes_keys_that_fit_their_postgres_columns(self):
        """SQLite ignores varchar lengths; PostgreSQL rejects the row.

        Production's first BYOK submission failed with "value too long for
        type character varying(64)": the ledger keyed its quote/reservation
        rows ``quote_<64-hex>`` / ``resv_<64-hex>``. Check every string the
        admission writes against its declared column length so this suite
        catches it without a PostgreSQL server.
        """
        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            _run_id, operation_id = self._submit(s, variants=2)
            rows = [
                *s.execute(
                    select(ProviderCostLedgerEntryRecord).where(
                        ProviderCostLedgerEntryRecord.operation_id == operation_id
                    )
                ).scalars(),
                *s.execute(
                    select(SpendReservationRecord).where(
                        SpendReservationRecord.operation_id == operation_id
                    )
                ).scalars(),
            ]
            # Two items x (image + quality check) x (quote + reservation) ledger
            # rows, plus their four reservations.
            self.assertEqual(len(rows), 12)
            for row in rows:
                for column in row.__table__.columns:
                    length = getattr(column.type, "length", None)
                    value = getattr(row, column.key, None)
                    if length and isinstance(value, str):
                        self.assertLessEqual(
                            len(value), length,
                            f"{row.__tablename__}.{column.name} = {value!r}",
                        )
            ledger_keys = {
                row.semantic_key_hash for row in rows
                if isinstance(row, ProviderCostLedgerEntryRecord)
            }
            # One quote + one reservation per call (image and quality check).
            self.assertEqual(len(ledger_keys), 8)

    def test_submission_is_refused_when_the_command_breaks_a_guardrail(self):
        from app.generation.submission import SubmissionError

        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            from app.generation.spend import ensure_spend_guardrail

            guardrail = ensure_spend_guardrail(s, self.ctx)
            guardrail.per_operation_limit = Decimal("0.05")
        with self.assertRaises(SubmissionError) as raised:
            with tenant_session(self.ctx) as s:
                self._submit(s, variants=2)
        self.assertEqual(raised.exception.code, "guardrail_exceeded")
        self.assertIn("per_operation_limit", raised.exception.detail)
        with tenant_session(self.ctx) as s:
            self.assertEqual(
                s.execute(select(GenerationRunRecord).where(
                    GenerationRunRecord.workspace_id == self.ctx.workspace_id
                )).scalars().all(),
                [],
            )

    # -- Worker dispatch ---------------------------------------------------

    def test_a_successful_call_commits_the_image_and_settles_the_reservation(self):
        version_id = self._bind_openai_key(b"sk-live-test")
        with tenant_session(self.ctx) as s:
            run_id, _ = self._submit(s, variants=1)
            item = self._items(s, run_id)[0]
            item_id = item.id

        adapter = _ScriptedAdapter(PNG)
        result = self._dispatch(adapter, item_id)
        self.assertTrue(result.success)
        request, credential = adapter.requests[0]
        self.assertEqual(credential, b"sk-live-test")
        self.assertEqual(request.model, "gpt-image-2")
        self.assertEqual(request.recipe.format.aspect_ratio, "1:1")
        self.assertEqual(request.recipe.product.name, "Test Product")
        self.assertFalse(request.recipe.use_reference_images)
        from app.generation.prompting import compose_text_prompt

        sent_prompt = compose_text_prompt(request.recipe)
        self.assertIn("Test Product", sent_prompt)

        with tenant_session(self.ctx) as s:
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "qc_pending")
            binding = s.execute(
                select(RunItemAssetVersionBindingRecord).where(
                    RunItemAssetVersionBindingRecord.run_item_id == item_id
                )
            ).scalar_one()
            version = s.get(AssetVersionRecord, binding.asset_version_id)
            digest = hashlib.sha256(PNG).hexdigest()
            self.assertEqual(version.content_sha256, digest)
            self.assertEqual(
                version.committed_object_key,
                f"committed/{self.ctx.workspace_id}/generation_output/{digest}.png",
            )
            self.assertEqual(version.committed_storage_version_id, "v1")
            self.assertEqual(version.byte_size, len(PNG))
            self.assertEqual(version.execution_mode, "byok")
            asset = s.get(AssetRecord, version.asset_id)
            self.assertEqual(asset.prompt, sent_prompt)
            self.assertEqual(version.reference_sources_json["used"], False)
            self.assertEqual(version.reference_sources_json["references"], [])
            attempt = s.execute(
                select(ProviderCallAttemptRecord).where(
                    ProviderCallAttemptRecord.run_item_id == item_id
                )
            ).scalar_one()
            self.assertEqual(attempt.dispatch_state, "succeeded")
            self.assertEqual(attempt.credential_version_id, version_id)
            self.assertEqual(attempt.provider_evidence_ref, version.committed_object_key)
            self.assertEqual(attempt.model, "gpt-image-2")
            reservation = self._reservations(s, item_id)[0]
            self.assertEqual(reservation.state, "settled")
            settlement = s.execute(
                select(ProviderCostLedgerEntryRecord).where(
                    ProviderCostLedgerEntryRecord.spend_reservation_id == reservation.id,
                    ProviderCostLedgerEntryRecord.entry_type == "estimated_settlement",
                )
            ).scalar_one()
            self.assertEqual(settlement.provider_amount, Decimal("0.053"))
            self.assertEqual(settlement.credential_version_id, version_id)
        self.assertEqual(len(self.blob_store.committed), 1)
        self.assertEqual(self.blob_store.committed[0][1], PNG)

    def test_a_terminal_refusal_fails_the_item_and_releases_the_reservation(self):
        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            run_id, _ = self._submit(s, variants=1)
            item_id = self._items(s, run_id)[0].id

        adapter = _ScriptedAdapter(ProviderDispatchError(
            "not_dispatched", "OpenAI 400: content policy", error_code="openai_400",
        ))
        result = self._dispatch(adapter, item_id)
        self.assertFalse(result.success)

        with tenant_session(self.ctx) as s:
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "failed")
            self.assertIn("openai_400", item.error_summary)
            self.assertIn("content policy", item.error_summary)
            self.assertEqual(self._reservations(s, item_id)[0].state, "released")
            # No image, nothing to check: the quality check's reservation goes too.
            self.assertEqual(self._reservations(s, item_id, quality_check=True)[0].state, "released")
            self.assertEqual(s.get(GenerationRunRecord, run_id).status, "failed")
        self.assertEqual(self.blob_store.committed, [])

    def test_an_unknown_outcome_parks_the_item_as_uncertain(self):
        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            run_id, _ = self._submit(s, variants=1)
            item_id = self._items(s, run_id)[0].id

        adapter = _ScriptedAdapter(ProviderDispatchError(
            "uncertain", "OpenAI request timed out", error_code="provider_timeout",
        ))
        self._dispatch(adapter, item_id)
        with tenant_session(self.ctx) as s:
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "uncertain")
            self.assertEqual(self._reservations(s, item_id)[0].state, "reserved")
            # Reconciliation may still produce an image to check.
            self.assertEqual(self._reservations(s, item_id, quality_check=True)[0].state, "reserved")
            self.assertEqual(s.get(GenerationRunRecord, run_id).status, "uncertain")

    def test_a_missing_key_pauses_the_item_without_calling_the_provider(self):
        version_id = self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            run_id, _ = self._submit(s, variants=1)
            item_id = self._items(s, run_id)[0].id
        with tenant_session(self.ctx) as s:
            CredentialAdmin(self.PROTECTOR).revoke(
                s, self.ctx, credential_key="OPENAI_API_KEY", reason="rotated out",
            )

        adapter = _ScriptedAdapter(PNG)
        self.assertIsNone(self._dispatch(adapter, item_id))
        self.assertEqual(adapter.requests, [])
        with tenant_session(self.ctx) as s:
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "paused")
            self.assertIn("credential_unavailable", item.error_summary)
            self.assertTrue(item.same_snapshot_resumable)
            # Nothing was dispatched, so the reservation is still open.
            self.assertEqual(self._reservations(s, item_id)[0].state, "reserved")
            # Resume reserves its check again under a fresh Job.
            self.assertEqual(self._reservations(s, item_id, quality_check=True)[0].state, "released")
            self.assertEqual(
                s.execute(select(ProviderCallAttemptRecord).where(
                    ProviderCallAttemptRecord.credential_version_id == version_id
                )).scalars().all(),
                [],
            )

    def test_retry_reserves_again_and_dispatches_as_the_next_attempt(self):
        from app.generation.recovery_preview import create_recovery_preview
        from app.generation.run_commands import retry_failed_items

        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            run_id, _ = self._submit(s, variants=1)
            item_id = self._items(s, run_id)[0].id

        self._dispatch(
            _ScriptedAdapter(ProviderDispatchError("not_dispatched", "nope", error_code="openai_400")),
            item_id,
        )
        with tenant_session(self.ctx) as s:
            run = s.get(GenerationRunRecord, run_id)
            preview = create_recovery_preview(
                s, self.ctx, run_id,
                command="retry_failed_items", selection_kind="all_eligible_failed", selection_item_ids=[],
                expected_run_revision=run.revision, idempotency_key=new_id("ik"),
            )
            self.assertEqual(Decimal(preview["estimated_additional_cost"]["amount"]), Decimal("0.053") + QC)
            run = s.get(GenerationRunRecord, run_id)
            retry = retry_failed_items(
                s, self.ctx, run_id=run_id, expected_revision=run.revision,
                preview_id=preview["id"], preview_hash=preview["preview_hash"],
                idempotency_key=new_id("ik"),
            )
            retry_operation_id = retry["operation_id"]
            reservations = self._reservations(s, item_id)
            self.assertEqual([r.state for r in reservations], ["released", "reserved"])
            self.assertEqual(reservations[1].operation_id, retry_operation_id)
            checks = self._reservations(s, item_id, quality_check=True)
            self.assertEqual([r.state for r in checks], ["released", "reserved"])
            self.assertEqual(checks[1].operation_id, retry_operation_id)

        adapter = _ScriptedAdapter(PNG)
        result = self._dispatch(adapter, item_id)
        self.assertTrue(result.success)
        with tenant_session(self.ctx) as s:
            self.assertEqual(s.get(RunItemRecord, item_id).state, "qc_pending")
            attempts = s.execute(
                select(ProviderCallAttemptRecord)
                .where(ProviderCallAttemptRecord.run_item_id == item_id)
                .order_by(ProviderCallAttemptRecord.attempt_no)
            ).scalars().all()
            self.assertEqual([a.attempt_no for a in attempts], [0, 1])
            self.assertEqual([a.dispatch_state for a in attempts], ["failed", "succeeded"])
            self.assertEqual([r.state for r in self._reservations(s, item_id)], ["released", "settled"])

    def test_the_prompt_carries_the_product_and_the_dimension_fragments(self):
        from app.generation.prompting import compose_item_prompt

        self._bind_openai_key()
        with tenant_session(self.ctx) as s:
            run_id, _ = self._submit(s, variants=1)
            item = self._items(s, run_id)[0]
            item.combination_identity_json = {
                "dimensions": {"lib_x:scene": "lifestyle", "lib_x:people": "with people"},
                "concepts": [
                    {
                        "dimension_key": "lib_x:scene",
                        "concept_key_id": "dck_1",
                        "value": {
                            "value": "lifestyle",
                            "positive_fragments": ["a bright kitchen counter at breakfast time"],
                            "negative_fragments": ["clutter"],
                        },
                    },
                ],
            }
            s.flush()
            run = s.get(GenerationRunRecord, run_id)
            prompt = compose_item_prompt(s, self.ctx, item, run)
        self.assertTrue(prompt.text.startswith("Professional advertising photograph of: Test Product"))
        self.assertIn("a bright kitchen counter at breakfast time.", prompt.text)
        self.assertIn("people: with people", prompt.text)
        self.assertNotIn("scene: lifestyle", prompt.text)
        self.assertTrue(prompt.text.endswith("Avoid: clutter."))
        self.assertIn("Square composition", prompt.text)


# ---------------------------------------------------------------------------
# Credential admin on column-level reads
# ---------------------------------------------------------------------------


class CredentialAdminTests(LibraryFixtures):
    def test_lifecycle_round_trip(self):
        ctx = self.owner_ctx(self._make_context())
        admin = CredentialAdmin(LocalKmsAdapter.local_only())
        with tenant_session(ctx) as s:
            created = admin.create(
                s, ctx, credential_key="OPENAI_API_KEY", provider="openai",
                plaintext=b"sk-one", display_label="Team key",
            )
            self.assertEqual(created.version_no, 1)
            with self.assertRaises(ValueError):
                admin.create(s, ctx, credential_key="OPENAI_API_KEY", provider="openai", plaintext=b"sk-dup")
            listed = admin.list_metadata(s, ctx)
            self.assertEqual([m.credential_version_id for m in listed], [created.credential_version_id])
            self.assertEqual(len(listed[0].value_sha256), 64)
            bound = admin.bind(
                s, ctx, binding_scope="generation", provider="openai",
                credential_version_id=created.credential_version_id,
            )
            self.assertEqual(bound["credential_version_id"], created.credential_version_id)
            with self.assertRaises(ValueError):
                admin.bind(
                    s, ctx, binding_scope="generation", provider="meta",
                    credential_version_id=created.credential_version_id,
                )
            rotated = admin.rotate(s, ctx, credential_key="OPENAI_API_KEY", new_plaintext=b"sk-two")
            self.assertEqual(rotated.version_no, 2)
            self.assertEqual([m.version_no for m in admin.list_metadata(s, ctx)], [2])
            admin.revoke(s, ctx, credential_key="OPENAI_API_KEY", reason="done")
            self.assertEqual(admin.list_metadata(s, ctx), [])
            with self.assertRaises(ValueError):
                admin.revoke(s, ctx, credential_key="OPENAI_API_KEY", reason="again")


if __name__ == "__main__":
    unittest.main()
