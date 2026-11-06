"""Issue #167 -- the provider-neutral CreativeRecipe, reference images and the
reconnected Gemini / Seedream adapters.

* The recipe is frozen at Preview (product brief, confirmed must-match only,
  the product's bound references primary first, test-plan directions) and
  rebuilt per RunItem at dispatch; recipes frozen before #167 rebuild the
  old way.
* The OpenAI text prompt is byte-identical to the pre-recipe prompt (golden).
* Reference use is a capability decision of the resolved model, shown in the
  preview and priced (new rule version) so Maximum cost == reservation.
* OpenAI sends references through ``/v1/images/edits`` (multipart); Gemini
  inline; Seedream as data URLs -- all through the coordinator
  (reserve -> call -> settle) with the Workspace's own key.
* The AssetVersion records which reference versions were sent.
* Capabilities advertise Gemini / Seedream only once their key is bound.
* Negative tenant: another Workspace's blob never reaches a provider.
"""
from __future__ import annotations

import asyncio
import base64
import copy
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
    LibraryEntryRecord,
    LibraryVersionRecord,
    ProductRecord,
    ReferenceBindingRecord,
    ReferenceObjectRecord,
    new_id,
)
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import LocalKmsAdapter
from app.entitlements.models import EntitlementProjectionRecord
from app.generation import pricing
from app.generation.creative_recipe import (
    CreativeRecipe,
    MustMatchItem,
    OutputFormat,
    ProductBrief,
    ReferenceImage,
    build_creative_recipe,
    confirmed_must_match,
    decide_reference_use,
    recipe_from_parts,
    reference_count_of,
)
from app.generation.gemini_adapter import GeminiImageAdapter
from app.generation.image_call import ByokGenerationRequest, ReferenceReader
from app.generation.models import (
    GenerationRecipeSnapshotRecord,
    RunItemAssetVersionBindingRecord,
    RunItemRecord,
)
from app.generation.openai_adapter import OpenAIImageAdapter
from app.generation.prompting import compose_item_prompt, compose_reference_prompt, compose_text_prompt
from app.generation.seedream_adapter import SeedreamImageAdapter
from app.generation.spend import reservation_semantic_key
from app.generation.vlm_qc import qc_max_cost
from app.platform.blob_store import CommittedBlobRef, GeneratedArtifactSpec
from app.providers.adapter import ProviderDispatchError
from app.providers.dispatch import ExternalCallCoordinator
from tests.support.library_fixtures import LibraryFixtures
from tests.test_test_plans_api import PlanFixtures

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00IEND\xaeB`\x82"
REF_FRONT = b"\xff\xd8\xff\xe0" + b"front-view-jpeg" * 8
REF_SIDE = b"\xff\xd8\xff\xe0" + b"side-view-jpeg" * 8
PROTECTOR = LocalKmsAdapter.local_only()

IDENTITY = {
    "dimensions": {"lib_x:scene": "lifestyle", "lib_x:people": "with people", "lib_x:hook": "h1"},
    "concepts": [
        {"dimension_key": "lib_x:scene", "concept_key_id": "dck_1",
         "value": {"value": "lifestyle",
                   "positive_fragments": ["a bright kitchen counter at breakfast time.", " warm morning light "],
                   "negative_fragments": ["clutter", "dark shadows."]}},
        {"dimension_key": "lib_x:hook", "concept_key_id": "dck_2",
         "value": {"value": "h1", "positive_fragments": ["close-up of the charging port"],
                   "negative_fragments": []}},
    ],
}

#: The prompts the Worker composed before the recipe existed (captured from
#: ``compose_item_prompt`` at 19a640d). They must never drift.
GOLDEN_BARE = (
    "Professional advertising photograph of: Test Product. a bright kitchen counter at breakfast "
    "time. warm morning light. close-up of the charging port. Scene attributes — people: with "
    "people. Square composition, the product is the clear hero of the image, commercial quality, "
    "photorealistic, no watermark, no gibberish text. Avoid: clutter; dark shadows."
)
GOLDEN_FULL = (
    "Professional advertising photograph of: VoltGo 65W Charger — Compact GaN wall charger. "
    "Target audience: Commuters who travel light. Key selling points: 65W fast charge; Foldable "
    "plug. a bright kitchen counter at breakfast time. warm morning light. close-up of the "
    "charging port. Scene attributes — people: with people. Brand constraints: Logo must stay "
    "upright. Tall vertical composition for mobile screens, the product is the clear hero of the "
    "image, commercial quality, photorealistic, no watermark, no gibberish text. Avoid: clutter; "
    "dark shadows."
)
GOLDEN_ODD = (
    "Professional advertising photograph of: Odd. a bright kitchen counter at breakfast time. "
    "warm morning light. close-up of the charging port. Scene attributes — people: with people. "
    "the product is the clear hero of the image, commercial quality, photorealistic, no "
    "watermark, no gibberish text. Avoid: clutter; dark shadows."
)


#: Issue #194: what every reference-image call asks for.
NO_COMPOSITE = "single continuous photograph, no split screen, no collage, no side-by-side panels"
_REFERENCE_TAIL = (
    "Change only the scene around it — setting, props, people, lighting, camera angle and "
    "composition — as described next. Output one single continuous photograph, no split screen, "
    "no collage, no side-by-side panels, no visible seam, and no frame or border of the reference "
    "photos. Professional advertising photograph of: VoltGo 65W Charger — Compact GaN wall "
    "charger. a desk by a window. Square composition, the product is the clear hero of the image, "
    "commercial quality, photorealistic, no watermark, no gibberish text. Avoid: clutter."
)
_KEEP = (
    "Keep the product exactly as it appears in the reference: identical shape, proportions, "
    "colors, materials, logo, markings and details. Do not redesign, restyle or replace it. "
)
GOLDEN_REFERENCE_TWO = (
    "The 2 attached images are reference photos of the same real product, VoltGo 65W Charger; "
    "the first one is the primary view. " + _KEEP
    + "It must match: Matte black housing; VoltGo logo on the front face. " + _REFERENCE_TAIL
)
GOLDEN_REFERENCE_ONE = (
    "The attached image is a reference photo of the real product, VoltGo 65W Charger. "
    + _KEEP + _REFERENCE_TAIL
)


def _qc(images: int) -> Decimal:
    """Issue #169: each image's quality check (GPT-5 mini, image + references)."""
    return qc_max_cost("openai", "gpt-5-mini", images)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _reference(ws: str, content: bytes, *, role: str = "primary", version: str = "libver_ref1") -> ReferenceImage:
    return ReferenceImage(
        reference_version_id=version,
        reference_entry_id="lib_ref",
        object_id="refobj_1",
        committed_key=f"committed/{ws}/reference_asset/{_sha(content)}.jpg",
        storage_version_id="v1",
        sha256=_sha(content),
        mime_type="image/jpeg",
        byte_size=len(content),
        purpose="product_reference",
        viewpoint="front",
        role=role,
    )


class _MemoryBlobStore:
    """Committed objects in memory: serves reads, records generated commits."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.reads: list[str] = []
        self.committed: list[tuple[GeneratedArtifactSpec, bytes]] = []

    async def open_committed(self, ref: CommittedBlobRef):
        self.reads.append(ref.committed_key)
        if ref.committed_key not in self.objects:
            raise FileNotFoundError(ref.committed_key)
        yield self.objects[ref.committed_key]

    async def commit_generated_artifact(self, source: Path, spec: GeneratedArtifactSpec) -> CommittedBlobRef:
        content = source.read_bytes()
        self.committed.append((spec, content))
        self.objects[spec.committed_key] = content
        return CommittedBlobRef(
            committed_key=spec.committed_key,
            storage_version_id=f"v{len(self.committed)}",
            size_bytes=len(content),
            checksum_sha256_b64=spec.expected_sha256_b64,
            content_type_verified=spec.content_type,
        )


def _recipe(ws: str = "ws_unit", *, references: tuple[ReferenceImage, ...] = (), use: bool = True,
            must_match: tuple[MustMatchItem, ...] = (), aspect: str = "1:1") -> CreativeRecipe:
    return CreativeRecipe(
        product=ProductBrief(name="VoltGo 65W Charger", description="Compact GaN wall charger."),
        format=OutputFormat(aspect_ratio=aspect),
        positive_fragments=("a desk by a window",),
        negative_constraints=("clutter",),
        must_match=must_match,
        reference_images=references,
        use_reference_images=use and bool(references),
        reference_image_count=len(references) if use else 0,
    )


# ---------------------------------------------------------------------------
# Pure translation, decision and pricing
# ---------------------------------------------------------------------------


class TestRecipeTranslation(unittest.TestCase):
    def test_text_prompt_is_byte_identical_to_the_pre_recipe_prompt(self):
        cases = (
            (ProductBrief(name="Test Product"), "1:1", GOLDEN_BARE),
            (
                ProductBrief(
                    name="VoltGo 65W Charger", description="Compact GaN wall charger.",
                    audience="Commuters who travel light", selling_points=("65W fast charge.", "Foldable plug"),
                    brand_notes="Logo must stay upright.",
                ),
                "9:16",
                GOLDEN_FULL,
            ),
            (ProductBrief(name="Odd"), "7:5", GOLDEN_ODD),
        )
        for brief, aspect, golden in cases:
            with self.subTest(aspect=aspect):
                legacy = recipe_from_parts(
                    creative=None, identity=IDENTITY, format_config={}, aspect_ratio=aspect,
                    legacy_product=brief,
                )
                self.assertFalse(legacy.frozen)
                self.assertEqual(compose_text_prompt(legacy), golden)
                frozen = recipe_from_parts(
                    creative={"product": {
                        "name": brief.name, "description": brief.description, "audience": brief.audience,
                        "selling_points": list(brief.selling_points), "brand_notes": brief.brand_notes,
                    }},
                    identity=IDENTITY, format_config={}, aspect_ratio=aspect,
                )
                self.assertTrue(frozen.frozen)
                self.assertEqual(compose_text_prompt(frozen), golden)

    def test_recipe_carries_values_fragments_and_negative_constraints(self):
        recipe = recipe_from_parts(
            creative=None, identity=IDENTITY, format_config={"quality": "high"}, aspect_ratio="1:1",
            legacy_product=ProductBrief(name="P"),
        )
        self.assertEqual(
            [(v.dimension_id, v.value, v.positive_fragments) for v in recipe.dimension_values],
            [
                ("scene", "lifestyle", ("a bright kitchen counter at breakfast time", "warm morning light")),
                ("people", "with people", ()),
                ("hook", "h1", ("close-up of the charging port",)),
            ],
        )
        self.assertEqual(recipe.negative_constraints, ("clutter", "dark shadows"))
        self.assertEqual(recipe.format.as_format_config(), {"aspect_ratio": "1:1", "quality": "high"})

    def test_reference_prompt_keeps_the_product_and_names_must_match_items(self):
        ws = "ws_unit"
        recipe = _recipe(
            ws,
            references=(_reference(ws, REF_FRONT), _reference(ws, REF_SIDE, role="secondary", version="libver_2")),
            must_match=(
                MustMatchItem(id="mm1", type="color", description="Matte black housing."),
                MustMatchItem(id="mm2", type="logo", description="VoltGo logo on the front face"),
            ),
        )
        prompt = compose_reference_prompt(recipe, 2)
        self.assertTrue(prompt.startswith("The 2 attached images are reference photos of the same real product"))
        self.assertIn("Keep the product exactly as it appears in the reference", prompt)
        self.assertIn("It must match: Matte black housing; VoltGo logo on the front face.", prompt)
        self.assertTrue(prompt.endswith(compose_text_prompt(recipe)))
        self.assertEqual(prompt, GOLDEN_REFERENCE_TWO)

    def test_reference_prompt_asks_for_one_continuous_photograph(self):
        # Issue #194: three references once came back as two pictures pasted
        # side by side; every reference call now forbids it.
        ws = "ws_unit"
        recipe = _recipe(ws, references=(_reference(ws, REF_FRONT),))
        prompt = compose_reference_prompt(recipe, 1)
        self.assertEqual(prompt, GOLDEN_REFERENCE_ONE)
        self.assertIn(NO_COMPOSITE, prompt)
        # The text-to-image prompt stays byte-identical (its own golden).
        self.assertNotIn(NO_COMPOSITE, compose_text_prompt(recipe))

    def test_every_reference_adapter_sends_the_no_composite_instruction(self):
        ws = "ws_unit"
        recipe = _recipe(ws, references=(_reference(ws, REF_FRONT), _reference(ws, REF_SIDE, version="libver_2")))
        for adapter, model in (
            (OpenAIImageAdapter(base_url="https://openai.test/v1", timeout=5), "gpt-image-2"),
            (GeminiImageAdapter(base_url="https://gemini.test/v1beta", timeout=5), "gemini-3.1-flash-image"),
            (SeedreamImageAdapter(base_url="https://ark.test/api/v3", timeout=5), "doubao-seedream-4-5-251128"),
        ):
            with self.subTest(adapter=type(adapter).__name__):
                plan = adapter.plan(_request(recipe, model))
                self.assertTrue(plan.references)
                self.assertEqual(plan.prompt, compose_reference_prompt(recipe, len(plan.references)))
                self.assertIn(NO_COMPOSITE, plan.prompt)

    def test_only_confirmed_must_match_items_enter_the_recipe(self):
        items = confirmed_must_match([
            {"id": "a", "type": "color", "description": "Black", "proposed_by": "ai", "confirmed": True},
            {"id": "b", "type": "logo", "description": "Logo", "proposed_by": "ai", "confirmed": False},
            {"id": "c", "type": "visual", "description": "Round edges", "proposed_by": "ai"},
            {"id": "d", "type": "text", "description": "Model name", "proposed_by": "user"},
            {"id": "e", "type": "other", "description": "Legacy item"},
            {"id": "f", "type": "other", "description": "Explicitly not", "proposed_by": "user", "confirmed": False},
            "a string is not a requirement",
            {"id": "g", "type": "other", "description": ""},
        ])
        self.assertEqual([item["id"] for item in items], ["a", "d", "e"])

    def test_reference_use_is_a_capability_decision(self):
        supporting = {"models": {"m": {"supports_reference_images": True, "max_reference_images": 2}}}
        plain = {"models": {"m": {}}}
        decision = decide_reference_use(supporting, "m", 3, execution_mode="byok")
        self.assertEqual((decision["use"], decision["count"], decision["reason"]), (True, 2, "reference_images_used"))
        self.assertEqual(
            decide_reference_use(plain, "m", 3, execution_mode="byok")["reason"],
            "model_does_not_support_reference_images",
        )
        self.assertEqual(
            decide_reference_use(supporting, "m", 0, execution_mode="byok")["reason"],
            "no_reference_images_bound",
        )
        mock = decide_reference_use(supporting, "m", 3, execution_mode="mock")
        self.assertEqual((mock["use"], mock["reason"]), (False, "execution_mode_ignores_reference_images"))
        self.assertEqual(reference_count_of({"creative": {"reference_policy": decision}}), 2)
        self.assertEqual(reference_count_of({"creative": {"reference_policy": mock}}), 0)
        self.assertEqual(reference_count_of({}), 0)


class TestPricing(unittest.TestCase):
    def test_reference_calls_are_priced_with_their_input_images(self):
        base = pricing.unit_price("openai", "gpt-image-2", {"aspect_ratio": "1:1"})
        self.assertEqual(base, Decimal("0.053"))
        self.assertEqual(
            pricing.unit_price("openai", "gpt-image-2", {"aspect_ratio": "1:1"}, reference_images=2),
            Decimal("0.053") + 2 * pricing.OPENAI_INPUT_IMAGE_PRICES["gpt-image-2"],
        )
        self.assertEqual(
            pricing.unit_price("gemini", "gemini-3.1-flash-image", {"aspect_ratio": "1:1"}, reference_images=1),
            Decimal("0.070") + Decimal("0.002"),
        )
        self.assertEqual(
            pricing.unit_price("gemini", "gemini-3.1-flash-image", {"image_size": "2K"}), Decimal("0.105"),
        )
        # Seedream does not bill input images separately.
        self.assertEqual(
            pricing.unit_price("seedream", "doubao-seedream-4-5-251128", {}, reference_images=3),
            Decimal("0.040"),
        )
        self.assertEqual(pricing.PRICE_RULE_VERSION, "image-prices-2026-10")
        with self.assertRaises(pricing.UnpricedCallError):
            pricing.unit_price("gemini", "gemini-unknown", {})
        with self.assertRaises(pricing.UnpricedCallError):
            pricing.unit_price("seedream", "doubao-seedream-3-0", {})


# ---------------------------------------------------------------------------
# Adapter HTTP contracts (fake transports)
# ---------------------------------------------------------------------------


def _request(recipe: CreativeRecipe, model: str, blob_store=None, ws: str = "ws_unit") -> ByokGenerationRequest:
    return ByokGenerationRequest(
        recipe=recipe, model=model, semantic_fingerprint="f" * 64,
        reference_reader=ReferenceReader(ws, blob_store),
    )


def _multipart_fields(request: httpx.Request) -> dict[str, list[bytes]]:
    content_type = request.headers["content-type"]
    boundary = content_type.split("boundary=", 1)[1].encode()
    fields: dict[str, list[bytes]] = {}
    for part in request.content.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, body = part.split(b"\r\n\r\n", 1)
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        fields.setdefault(name, []).append(body.rsplit(b"\r\n", 1)[0])
    return fields


class TestOpenAIEdits(unittest.IsolatedAsyncioTestCase):
    async def test_references_go_to_image_edits_as_multipart(self):
        ws = "ws_unit"
        refs = (_reference(ws, REF_FRONT), _reference(ws, REF_SIDE, role="secondary", version="libver_2"))
        store = _MemoryBlobStore({ref.committed_key: content for ref, content in zip(refs, (REF_FRONT, REF_SIDE))})
        recipe = _recipe(ws, references=refs, must_match=(
            MustMatchItem(id="mm1", type="color", description="Matte black housing"),
        ))
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["Authorization"]
            seen["fields"] = _multipart_fields(request)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        adapter = OpenAIImageAdapter(base_url="https://openai.test/v1", timeout=5,
                                     transport=httpx.MockTransport(handler))
        request = _request(recipe, "gpt-image-1", store)
        plan = adapter.plan(request)
        result = await adapter.execute(request, credential=memoryview(b"sk-ws"), idempotency_key="k")

        self.assertEqual(seen["url"], "https://openai.test/v1/images/edits")
        self.assertEqual(seen["auth"], "Bearer sk-ws")
        fields = seen["fields"]
        self.assertEqual(fields["image[]"], [REF_FRONT, REF_SIDE])  # primary first
        self.assertEqual(fields["model"], [b"gpt-image-1"])
        self.assertEqual(fields["size"], [b"1024x1024"])
        self.assertEqual(fields["quality"], [b"medium"])
        self.assertEqual(fields["n"], [b"1"])
        self.assertEqual(fields["input_fidelity"], [b"high"])
        sent_prompt = fields["prompt"][0].decode()
        self.assertEqual(sent_prompt, plan.prompt)
        self.assertIn("It must match: Matte black housing.", sent_prompt)
        self.assertIn("a desk by a window", sent_prompt)
        self.assertEqual(plan.references, refs)
        self.assertEqual(result.output.content_bytes, PNG)
        self.assertEqual(store.reads, [ref.committed_key for ref in refs])

    async def test_without_references_the_text_generation_call_is_unchanged(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        adapter = OpenAIImageAdapter(base_url="https://openai.test/v1", timeout=5,
                                     transport=httpx.MockTransport(handler))
        recipe = _recipe(references=(_reference("ws_unit", REF_FRONT),), use=False, aspect="16:9")
        await adapter.execute(_request(recipe, "gpt-image-2"), credential=memoryview(b"k"), idempotency_key="k")
        self.assertEqual(seen["url"], "https://openai.test/v1/images/generations")
        self.assertEqual(seen["body"], {
            "model": "gpt-image-2", "prompt": compose_text_prompt(recipe),
            "size": "1536x1024", "quality": "medium", "n": 1,
        })

    async def test_unreadable_or_foreign_references_are_never_sent(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        adapter = OpenAIImageAdapter(base_url="https://openai.test/v1", timeout=5,
                                     transport=httpx.MockTransport(handler))
        foreign = _reference("ws_other", REF_FRONT)
        store = _MemoryBlobStore({foreign.committed_key: REF_FRONT})
        with self.assertRaises(ProviderDispatchError) as raised:
            await adapter.execute(
                _request(_recipe("ws_unit", references=(foreign,)), "gpt-image-2", store, ws="ws_unit"),
                credential=memoryview(b"k"), idempotency_key="k",
            )
        self.assertEqual(raised.exception.dispatch_certainty, "not_dispatched")
        self.assertEqual(raised.exception.error_code, "reference_not_in_workspace")
        self.assertEqual(store.reads, [])

        tampered = _reference("ws_unit", REF_FRONT)
        store = _MemoryBlobStore({tampered.committed_key: REF_SIDE})
        with self.assertRaises(ProviderDispatchError) as raised:
            await adapter.execute(
                _request(_recipe("ws_unit", references=(tampered,)), "gpt-image-2", store),
                credential=memoryview(b"k"), idempotency_key="k",
            )
        self.assertEqual(raised.exception.error_code, "reference_changed")

        with self.assertRaises(ProviderDispatchError) as raised:
            await adapter.execute(
                _request(_recipe("ws_unit", references=(tampered,)), "gpt-image-2", _MemoryBlobStore()),
                credential=memoryview(b"k"), idempotency_key="k",
            )
        self.assertEqual(raised.exception.error_code, "reference_unavailable")
        self.assertEqual(calls, [])


class TestGeminiAndSeedreamAdapters(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_sends_the_prompt_then_the_references_inline(self):
        ws = "ws_unit"
        ref = _reference(ws, REF_FRONT)
        store = _MemoryBlobStore({ref.committed_key: REF_FRONT})
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["key"] = request.headers["x-goog-api-key"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [
                {"text": "here"}, {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}},
            ]}}], "usageMetadata": {"totalTokenCount": 1500}})

        adapter = GeminiImageAdapter(base_url="https://gemini.test/v1beta", timeout=5,
                                     transport=httpx.MockTransport(handler))
        recipe = _recipe(ws, references=(ref,), aspect="4:5")
        request = _request(recipe, "gemini-3.1-flash-image", store)
        result = await adapter.execute(request, credential=memoryview(b"g-key"), idempotency_key="k")

        self.assertEqual(seen["url"], "https://gemini.test/v1beta/models/gemini-3.1-flash-image:generateContent")
        self.assertEqual(seen["key"], "g-key")
        parts = seen["body"]["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": adapter.plan(request).prompt})
        self.assertIn("Keep the product exactly", parts[0]["text"])
        self.assertEqual(base64.b64decode(parts[1]["inline_data"]["data"]), REF_FRONT)
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/jpeg")
        self.assertEqual(seen["body"]["generationConfig"]["imageConfig"], {"aspectRatio": "4:5", "imageSize": "1K"})
        self.assertEqual(result.output.content_bytes, PNG)
        self.assertEqual(result.output.usage, {"totalTokenCount": 1500})

    async def test_gemini_failure_classification(self):
        async def run(response: httpx.Response) -> ProviderDispatchError:
            adapter = GeminiImageAdapter(base_url="https://gemini.test/v1beta", timeout=5,
                                         transport=httpx.MockTransport(lambda r: response))
            with self.assertRaises(ProviderDispatchError) as raised:
                await adapter.execute(_request(_recipe(), "gemini-3.1-flash-image"),
                                      credential=memoryview(b"k"), idempotency_key="k")
            return raised.exception

        blocked = await run(httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
        self.assertEqual((blocked.dispatch_certainty, blocked.error_code), ("not_dispatched", "provider_refused"))
        empty = await run(httpx.Response(200, json={"candidates": [
            {"finishReason": "IMAGE_SAFETY", "content": {"parts": [{"text": "cannot"}]}}]}))
        self.assertEqual((empty.dispatch_certainty, empty.error_code), ("dispatched", "no_image_returned"))
        self.assertIn("IMAGE_SAFETY", str(empty))
        rejected = await run(httpx.Response(403, json={"error": {"message": "bad key"}}))
        self.assertEqual((rejected.dispatch_certainty, rejected.error_code), ("not_dispatched", "credential_rejected"))
        server = await run(httpx.Response(503, text="overloaded"))
        self.assertEqual((server.dispatch_certainty, server.error_code), ("uncertain", "gemini_503"))

    async def test_seedream_sends_references_as_data_urls_at_2k(self):
        ws = "ws_unit"
        refs = (_reference(ws, REF_FRONT), _reference(ws, REF_SIDE, role="secondary", version="libver_2"))
        store = _MemoryBlobStore({ref.committed_key: c for ref, c in zip(refs, (REF_FRONT, REF_SIDE))})
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["Authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        adapter = SeedreamImageAdapter(base_url="https://ark.test/api/v3", timeout=5,
                                       transport=httpx.MockTransport(handler))
        request = _request(_recipe(ws, references=refs, aspect="9:16"), "doubao-seedream-4-5-251128", store)
        result = await adapter.execute(request, credential=memoryview(b"ark-key"), idempotency_key="k")
        self.assertEqual(seen["url"], "https://ark.test/api/v3/images/generations")
        self.assertEqual(seen["auth"], "Bearer ark-key")
        body = seen["body"]
        self.assertEqual(body["model"], "doubao-seedream-4-5-251128")
        self.assertEqual(body["size"], "1440x2560")
        self.assertEqual(body["prompt"], adapter.plan(request).prompt)
        self.assertEqual(
            body["image"],
            [f"data:image/jpeg;base64,{base64.b64encode(c).decode()}" for c in (REF_FRONT, REF_SIDE)],
        )
        self.assertFalse(body["watermark"])
        self.assertEqual(result.output.content_bytes, PNG)


# ---------------------------------------------------------------------------
# The Run chain: freeze at Preview, reserve at Submission, dispatch, lineage
# ---------------------------------------------------------------------------


class RecipeChainTests(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self.owner_ctx(self._make_context(role="owner", actor="owner_a"))
        with tenant_session(self.ctx) as s:
            s.add(EntitlementProjectionRecord(
                workspace_id=self.ctx.workspace_id,
                entitlement_version=1, lease_sequence=1, projection_version=1,
                semantic_hash="hash_recipe", resolved_entitlements_json={"features": ["generation"]},
            ))
        self.blob_store = _MemoryBlobStore()
        self.coordinator = ExternalCallCoordinator(session_factory=tenant_session)

    # -- fixtures ---------------------------------------------------------

    def _bind(self, provider: str, credential_key: str, plaintext: bytes = b"sk-ws-own", ctx=None) -> str:
        ctx = ctx or self.ctx
        admin = CredentialAdmin(PROTECTOR)
        with tenant_session(ctx) as s:
            meta = admin.create(s, ctx, credential_key=credential_key, provider=provider, plaintext=plaintext)
            admin.bind(s, ctx, binding_scope="generation", provider=provider,
                       credential_version_id=meta.credential_version_id)
            return meta.credential_version_id

    def _library_version(self, s, ctx, entry_type: str, content: dict, *, name: str) -> tuple[str, str]:
        entry = LibraryEntryRecord(
            id=new_id("lib"), workspace_id=ctx.workspace_id, entry_type=entry_type, display_name=name,
        )
        s.add(entry)
        s.flush()
        version = LibraryVersionRecord(
            id=new_id("libver"), workspace_id=ctx.workspace_id, entry_id=entry.id, entry_type=entry_type,
            version_no=1, content_json=content, content_hash=_sha(json.dumps(content, sort_keys=True).encode()),
            published_by="owner_a",
        )
        s.add(version)
        s.flush()
        entry.current_version_id = version.id
        s.flush()
        return entry.id, version.id

    def _product_with_references(
        self, refs: list[tuple[bytes, str, int]], *, must_match: list[dict] | None = None,
        ctx=None, derivative_workspace: str | None = None,
    ) -> tuple[str, list[str]]:
        """A published ProductVersion with ``refs`` = [(bytes, viewpoint, priority)] bound."""
        ctx = ctx or self.ctx
        with tenant_session(ctx) as s:
            entry_id, product_version_id = self._library_version(s, ctx, "product", {
                "name": "VoltGo 65W Charger",
                "description": "Compact GaN wall charger",
                "audience": "Commuters",
                "selling_points": ["65W"],
                "must_match": must_match or [],
            }, name="VoltGo")
            product = ProductRecord(
                id=new_id("prod"), workspace_id=ctx.workspace_id, name="VoltGo 65W Charger",
                product_profile_entry_id=entry_id,
            )
            s.add(product)
            reference_version_ids: list[str] = []
            for position, (content, viewpoint, priority) in enumerate(refs):
                digest = _sha(content)
                owner_ws = derivative_workspace or ctx.workspace_id
                key = f"committed/{owner_ws}/reference_asset/{digest}.jpg"
                obj = ReferenceObjectRecord(
                    id=new_id("refobj"), workspace_id=ctx.workspace_id, content_sha256=digest,
                    original_object_key=f"committed/{ctx.workspace_id}/reference_original/{digest}.jpg",
                    declared_mime="image/jpeg", mime_type="image/jpeg", byte_size=len(content),
                    state="released", derivative_object_key=key, derivative_storage_version_id="v1",
                    derivative_sha256=digest, derivative_byte_size=len(content), derivative_mime="image/jpeg",
                    derivative_width=800, derivative_height=800,
                )
                s.add(obj)
                s.flush()
                _ref_entry, ref_version = self._library_version(
                    s, ctx, "reference_asset", {"object_id": obj.id, "source": "upload"}, name=f"ref {viewpoint}",
                )
                s.add(ReferenceBindingRecord(
                    id=new_id("rb"), workspace_id=ctx.workspace_id, owner_version_id=product_version_id,
                    reference_version_id=ref_version, purpose="product_reference", viewpoint=viewpoint,
                    priority=priority, position=position,
                ))
                self.blob_store.objects[key] = content
                reference_version_ids.append(ref_version)
            s.flush()
            return product.id, reference_version_ids

    def _draft(self, s, product_id: str, *, provider: str = "openai", model: str = "gpt-image-2",
               mode: str = "byok", variants: int = 1, aspect_ratio: str = "1:1", strategy: dict | None = None):
        from app.generation.draft import create_draft

        return create_draft(
            s, self.ctx, product_id=product_id, provider=provider, model=model, execution_mode=mode,
            combination_strategy=strategy or {"kind": "full", "planner_version": "v1"},
            format_config={"aspect_ratio": aspect_ratio, "variants_per_combination": variants},
        )

    def _preview(self, s, draft, **kwargs):
        from app.generation.preview import create_preview

        return create_preview(s, self.ctx, draft.id, draft.revision, **kwargs)

    def _submit(self, s, draft, preview) -> str:
        from app.generation.submission import submit_generation

        return submit_generation(
            s, self.ctx, preview_id=preview.id, preview_hash=preview.preview_hash,
            expected_draft_revision=draft.revision, idempotency_key=new_id("ik"),
        )["generation_run_id"]

    def _item(self, s, run_id: str) -> RunItemRecord:
        return s.execute(
            select(RunItemRecord).where(RunItemRecord.generation_run_id == run_id).order_by(RunItemRecord.ordinal)
        ).scalars().first()

    def _dispatch(self, adapters: dict, item_id: str):
        from app.generation.byok_dispatch import handle_run_item_byok

        return asyncio.run(handle_run_item_byok(
            self.coordinator, self.ctx, item_id, adapters=adapters, blob_store=self.blob_store,
            protector=PROTECTOR, session_factory=tenant_session,
        ))

    def _reservation(self, s, item_id: str) -> SpendReservationRecord:
        """The image call's reservation (the quality check reserves its own, #169)."""
        item = s.get(RunItemRecord, item_id)
        job = s.execute(
            select(JobRecord).where(JobRecord.resource_type == "run_item", JobRecord.resource_id == item_id)
        ).scalars().one()
        return s.execute(
            select(SpendReservationRecord).where(
                SpendReservationRecord.semantic_key_hash == reservation_semantic_key(item.semantic_fingerprint, job.id)
            )
        ).scalars().one()

    def _produced_version(self, s, item_id: str) -> AssetVersionRecord:
        binding = s.execute(
            select(RunItemAssetVersionBindingRecord).where(RunItemAssetVersionBindingRecord.run_item_id == item_id)
        ).scalar_one()
        return s.get(AssetVersionRecord, binding.asset_version_id)

    # -- freeze -----------------------------------------------------------

    def test_preview_freezes_brief_confirmed_must_match_and_references_primary_first(self):
        self._bind("openai", "OPENAI_API_KEY")
        product_id, (side_id, front_id) = self._product_with_references(
            [(REF_SIDE, "side", 1), (REF_FRONT, "front", 0)],
            must_match=[
                {"id": "mm-color", "type": "color", "description": "Matte black", "severity": "blocking",
                 "viewpoints": ["front"], "qc_method": "human_review", "proposed_by": "ai", "confirmed": True},
                {"id": "mm-logo", "type": "logo", "description": "Logo top-left", "severity": "warning",
                 "viewpoints": ["front"], "qc_method": "human_review", "proposed_by": "ai", "confirmed": False},
            ],
        )
        with tenant_session(self.ctx) as s:
            draft = self._draft(s, product_id, variants=2)
            preview = self._preview(s, draft)
            creative = preview.snapshot_json["recipe"]["creative"]
            self.assertEqual(creative["product"]["name"], "VoltGo 65W Charger")
            self.assertTrue(creative["product"]["version_id"])
            self.assertEqual([m["id"] for m in creative["must_match"]], ["mm-color"])
            self.assertEqual(
                [(r["reference_version_id"], r["role"], r["viewpoint"]) for r in creative["reference_images"]],
                [(front_id, "primary", "front"), (side_id, "secondary", "side")],
            )
            self.assertEqual(creative["reference_exclusions"], [])
            self.assertIsNone(creative["test_plan_id"])
            policy = creative["reference_policy"]
            self.assertEqual((policy["use"], policy["count"]), (True, 2))

            projections = preview.snapshot_json["ui_projections"]
            decision = [d for d in projections["capability_decisions"] if d["capability"] == "reference_images"]
            self.assertEqual(len(decision), 1)
            self.assertTrue(decision[0]["supported"])
            per_call = Decimal("0.053") + 2 * pricing.OPENAI_INPUT_IMAGE_PRICES["gpt-image-2"]
            # Issue #169: plus each image's quality check against both references.
            self.assertEqual(Decimal(projections["maximum_cost"]["amount"]), (per_call + _qc(3)) * 2)
            self.assertEqual(preview.snapshot_json["recipe"]["cost_policy"]["reference_images_per_call"], 2)

            run_id = self._submit(s, draft, preview)
            run = s.get(GenerationRunRecord, run_id)
            self.assertEqual(run.est_cost, (per_call + _qc(3)) * 2)
            self.assertTrue(run.use_ref)
            item = self._item(s, run_id)
            self.assertEqual(self._reservation(s, item.id).maximum_provider_amount, per_call)

            recipe = build_creative_recipe(s, self.ctx, item, run)
            self.assertTrue(recipe.frozen)
            self.assertTrue(recipe.use_reference_images)
            self.assertEqual([r.reference_version_id for r in recipe.references_to_send], [front_id, side_id])
            self.assertEqual([m.id for m in recipe.must_match], ["mm-color"])
            self.assertIsNone(recipe.direction)

    def test_without_references_the_recipe_is_text_only_and_priced_as_before(self):
        self._bind("openai", "OPENAI_API_KEY")
        with tenant_session(self.ctx) as s:
            product_id = self._product_id(self.ctx, s)
            draft = self._draft(s, product_id)
            preview = self._preview(s, draft)
            creative = preview.snapshot_json["recipe"]["creative"]
            self.assertEqual(creative["reference_images"], [])
            self.assertEqual(creative["reference_policy"]["reason"], "no_reference_images_bound")
            self.assertEqual(
                Decimal(preview.snapshot_json["ui_projections"]["maximum_cost"]["amount"]),
                Decimal("0.053") + _qc(1),
            )
            run_id = self._submit(s, draft, preview)
            run = s.get(GenerationRunRecord, run_id)
            self.assertFalse(run.use_ref)
            item = self._item(s, run_id)
            recipe = build_creative_recipe(s, self.ctx, item, run)
            self.assertFalse(recipe.use_reference_images)
            # The frozen recipe composes the same prompt the legacy path does.
            legacy = recipe_from_parts(
                creative=None, identity=item.combination_identity_json, format_config={},
                aspect_ratio=run.aspect_ratio, legacy_product=ProductBrief(name="Test Product"),
            )
            self.assertEqual(compose_text_prompt(recipe), compose_text_prompt(legacy))

    def test_mock_and_unsupported_models_never_send_references(self):
        from app.generation.capabilities import PLATFORM_ADAPTER_REGISTRY

        product_id, _ = self._product_with_references([(REF_FRONT, "front", 0)])
        with tenant_session(self.ctx) as s:
            mock_preview = self._preview(s, self._draft(s, product_id, provider="mock", model="mock-v1", mode="mock"))
            policy = mock_preview.snapshot_json["recipe"]["creative"]["reference_policy"]
            self.assertEqual((policy["use"], policy["reason"]), (False, "execution_mode_ignores_reference_images"))
            decision = [d for d in mock_preview.snapshot_json["ui_projections"]["capability_decisions"]
                        if d["capability"] == "reference_images"][0]
            self.assertFalse(decision["supported"])
            self.assertEqual(mock_preview.snapshot_json["ui_projections"]["maximum_cost"]["amount"], "0")

        self._bind("openai", "OPENAI_API_KEY")
        registry = copy.deepcopy(PLATFORM_ADAPTER_REGISTRY)
        registry["openai"]["models"]["gpt-image-2"]["supports_reference_images"] = False
        with tenant_session(self.ctx) as s:
            preview = self._preview(s, self._draft(s, product_id), adapter_registry=registry)
            policy = preview.snapshot_json["recipe"]["creative"]["reference_policy"]
            self.assertEqual((policy["use"], policy["reason"]), (False, "model_does_not_support_reference_images"))
            # The quality check still compares the image with the reference.
            self.assertEqual(
                Decimal(preview.snapshot_json["ui_projections"]["maximum_cost"]["amount"]),
                Decimal("0.053") + _qc(2),
            )

    def test_a_recipe_frozen_before_the_creative_block_rebuilds_the_legacy_way(self):
        self._bind("openai", "OPENAI_API_KEY")
        with tenant_session(self.ctx) as s:
            product_id = self._product_id(self.ctx, s)
            draft = self._draft(s, product_id)
            run_id = self._submit(s, draft, self._preview(s, draft))
            run = s.get(GenerationRunRecord, run_id)
            snapshot = s.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id)
            legacy = dict(snapshot.canonical_json)
            legacy.pop("creative")
            snapshot.canonical_json = legacy
            s.flush()
            item = self._item(s, run_id)
            item.combination_identity_json = IDENTITY
            s.flush()
            recipe = build_creative_recipe(s, self.ctx, item, run)
            self.assertFalse(recipe.frozen)
            self.assertEqual(recipe.reference_images, ())
            self.assertEqual(compose_item_prompt(s, self.ctx, item, run).text, GOLDEN_BARE)

    # -- dispatch ---------------------------------------------------------

    def test_openai_edits_through_the_coordinator_record_reference_lineage(self):
        version_id = self._bind("openai", "OPENAI_API_KEY", b"sk-live")
        product_id, (front_id, side_id) = self._product_with_references(
            [(REF_FRONT, "front", 0), (REF_SIDE, "side", 1)],
            must_match=[{"id": "mm-color", "type": "color", "description": "Matte black", "severity": "blocking",
                         "viewpoints": ["front"], "qc_method": "human_review", "proposed_by": "user"}],
        )
        with tenant_session(self.ctx) as s:
            draft = self._draft(s, product_id)
            run_id = self._submit(s, draft, self._preview(s, draft))
            item_id = self._item(s, run_id).id

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, headers={"x-request-id": "req_edit"},
                                  json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        adapter = OpenAIImageAdapter(base_url="https://openai.test/v1", timeout=5,
                                     transport=httpx.MockTransport(handler))
        result = self._dispatch({"openai": adapter}, item_id)
        self.assertTrue(result.success, result.error_detail)
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://openai.test/v1/images/edits")
        fields = _multipart_fields(requests[0])
        self.assertEqual(fields["image[]"], [REF_FRONT, REF_SIDE])

        per_call = Decimal("0.053") + 2 * pricing.OPENAI_INPUT_IMAGE_PRICES["gpt-image-2"]
        with tenant_session(self.ctx) as s:
            version = self._produced_version(s, item_id)
            sources = version.reference_sources_json
            self.assertTrue(sources["used"])
            self.assertEqual(sources["decision"], "reference_images_used")
            self.assertEqual(
                [(r["reference_version_id"], r["role"]) for r in sources["references"]],
                [(front_id, "primary"), (side_id, "secondary")],
            )
            self.assertEqual(sources["references"][0]["sha256"], _sha(REF_FRONT))
            self.assertEqual(sources["must_match_ids"], ["mm-color"])
            self.assertNotIn("committed_key", json.dumps(sources))
            asset = s.get(AssetRecord, version.asset_id)
            self.assertEqual(asset.prompt, fields["prompt"][0].decode())
            self.assertIn("It must match: Matte black.", asset.prompt)
            reservation = self._reservation(s, item_id)
            self.assertEqual(reservation.state, "settled")
            settlement = s.execute(
                select(ProviderCostLedgerEntryRecord).where(
                    ProviderCostLedgerEntryRecord.spend_reservation_id == reservation.id,
                    ProviderCostLedgerEntryRecord.entry_type == "estimated_settlement",
                )
            ).scalar_one()
            self.assertEqual(settlement.provider_amount, per_call)
            attempt = s.execute(
                select(ProviderCallAttemptRecord).where(ProviderCallAttemptRecord.run_item_id == item_id)
            ).scalar_one()
            self.assertEqual(attempt.credential_version_id, version_id)

            from app.generation.asset_queries import get_asset_version_detail

            detail = get_asset_version_detail(s, self.ctx, version.id)
            self.assertEqual(detail["reference_sources"], sources)

    def test_gemini_byok_runs_reserve_call_and_settle(self):
        product_id, (front_id,) = self._product_with_references([(REF_FRONT, "front", 0)])
        self._bind("gemini", "GEMINI_API_KEY", b"g-live")
        with tenant_session(self.ctx) as s:
            draft = self._draft(s, product_id, provider="gemini", model="gemini-3.1-flash-image", aspect_ratio="4:5")
            preview = self._preview(s, draft)
            self.assertEqual(preview.snapshot_json["ui_projections"]["maximum_cost"]["amount"], "0.072")
            run_id = self._submit(s, draft, preview)
            item_id = self._item(s, run_id).id
            self.assertEqual(self._reservation(s, item_id).maximum_provider_amount, Decimal("0.072"))

        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["x-goog-api-key"], "g-live")
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [
                {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(PNG).decode()}}]}}]})

        adapter = GeminiImageAdapter(base_url="https://gemini.test/v1beta", timeout=5,
                                     transport=httpx.MockTransport(handler))
        result = self._dispatch({"gemini": adapter}, item_id)
        self.assertTrue(result.success, result.error_detail)
        parts = bodies[0]["contents"][0]["parts"]
        self.assertEqual(base64.b64decode(parts[1]["inline_data"]["data"]), REF_FRONT)
        with tenant_session(self.ctx) as s:
            self.assertEqual(s.get(RunItemRecord, item_id).state, "qc_pending")
            reservation = self._reservation(s, item_id)
            self.assertEqual(reservation.state, "settled")
            version = self._produced_version(s, item_id)
            self.assertEqual(version.reference_sources_json["references"][0]["reference_version_id"], front_id)
            attempt = s.execute(
                select(ProviderCallAttemptRecord).where(ProviderCallAttemptRecord.run_item_id == item_id)
            ).scalar_one()
            self.assertEqual((attempt.provider, attempt.model), ("gemini", "gemini-3.1-flash-image"))

    def test_gemini_answer_without_an_image_fails_the_item_terminally(self):
        self._bind("gemini", "GEMINI_API_KEY")
        with tenant_session(self.ctx) as s:
            product_id = self._product_id(self.ctx, s)
            draft = self._draft(s, product_id, provider="gemini", model="gemini-3.1-flash-image")
            run_id = self._submit(s, draft, self._preview(s, draft))
            item_id = self._item(s, run_id).id
        adapter = GeminiImageAdapter(
            base_url="https://gemini.test/v1beta", timeout=5,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"candidates": [
                {"finishReason": "IMAGE_SAFETY", "content": {"parts": [{"text": "no"}]}}]})),
        )
        result = self._dispatch({"gemini": adapter}, item_id)
        self.assertFalse(result.success)
        with tenant_session(self.ctx) as s:
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "failed")
            self.assertIn("no_image_returned", item.error_summary)
            self.assertEqual(self._reservation(s, item_id).state, "settled")

    def test_seedream_byok_runs_reserve_call_and_settle(self):
        self._bind("seedream", "ARK_API_KEY", b"ark-live")
        with tenant_session(self.ctx) as s:
            product_id = self._product_id(self.ctx, s)
            draft = self._draft(s, product_id, provider="seedream", model="doubao-seedream-4-5-251128")
            preview = self._preview(s, draft)
            self.assertEqual(preview.snapshot_json["ui_projections"]["maximum_cost"]["amount"], "0.04")
            run_id = self._submit(s, draft, preview)
            item_id = self._item(s, run_id).id

        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["Authorization"], "Bearer ark-live")
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        adapter = SeedreamImageAdapter(base_url="https://ark.test/api/v3", timeout=5,
                                       transport=httpx.MockTransport(handler))
        result = self._dispatch({"seedream": adapter}, item_id)
        self.assertTrue(result.success, result.error_detail)
        self.assertNotIn("image", bodies[0])
        self.assertEqual(bodies[0]["size"], "2048x2048")
        with tenant_session(self.ctx) as s:
            self.assertEqual(self._reservation(s, item_id).state, "settled")
            version = self._produced_version(s, item_id)
            self.assertFalse(version.reference_sources_json["used"])
            asset = s.get(AssetRecord, version.asset_id)
            self.assertEqual(asset.prompt, bodies[0]["prompt"])
            self.assertEqual(asset.provider, "seedream")

    # -- capabilities -----------------------------------------------------

    def test_capabilities_advertise_gemini_and_seedream_only_with_their_key(self):
        from app.generation.capabilities import get_capabilities

        with tenant_session(self.ctx) as s:
            before = get_capabilities(s, self.ctx)
        self.assertIn("openai", before["providers"])
        self.assertNotIn("gemini", before["providers"])
        self.assertNotIn("seedream", before["providers"])
        self.assertTrue(before["models"]["openai"]["gpt-image-2"]["supports_reference_images"])

        self._bind("gemini", "GEMINI_API_KEY")
        with tenant_session(self.ctx) as s:
            after = get_capabilities(s, self.ctx)
        self.assertIn("gemini", after["providers"])
        self.assertNotIn("seedream", after["providers"])
        self.assertIn("byok", after["modes"])
        model = after["models"]["gemini"]["gemini-3.1-flash-image"]
        self.assertTrue(model["supports_reference_images"])
        self.assertEqual(model["max_reference_images"], 4)

        other = self.owner_ctx(self._make_context(role="owner"))
        with tenant_session(other) as s:
            self.assertNotIn("gemini", get_capabilities(s, other)["providers"])

    # -- negative tenant --------------------------------------------------

    def test_another_workspaces_reference_blob_is_never_used(self):
        other = self.owner_ctx(self._make_context(role="owner"))
        # A reference object of this Workspace whose derivative key points
        # into another Workspace's prefix is excluded at freeze time.
        self._bind("openai", "OPENAI_API_KEY")
        product_id, (bad_id,) = self._product_with_references(
            [(REF_FRONT, "front", 0)], derivative_workspace=other.workspace_id,
        )
        with tenant_session(self.ctx) as s:
            draft = self._draft(s, product_id)
            preview = self._preview(s, draft)
            creative = preview.snapshot_json["recipe"]["creative"]
            self.assertEqual(creative["reference_images"], [])
            self.assertEqual(
                creative["reference_exclusions"],
                [{"reference_version_id": bad_id, "reason": "reference_derivative_not_committed"}],
            )
            self.assertEqual(
                Decimal(preview.snapshot_json["ui_projections"]["maximum_cost"]["amount"]),
                Decimal("0.053") + _qc(1),
            )

        # The other Workspace's product never sees this Workspace's references.
        with tenant_session(other) as s:
            s.add(ProductRecord(id=new_id("prod"), workspace_id=other.workspace_id, name="Other"))

        # A tampered snapshot naming a foreign blob is refused before any call.
        good_product, _ = self._product_with_references([(REF_SIDE, "front", 0)])
        with tenant_session(self.ctx) as s:
            draft = self._draft(s, good_product)
            run_id = self._submit(s, draft, self._preview(s, draft))
            run = s.get(GenerationRunRecord, run_id)
            snapshot = s.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id)
            tampered = copy.deepcopy(snapshot.canonical_json)
            foreign_key = f"committed/{other.workspace_id}/reference_asset/{_sha(REF_SIDE)}.jpg"
            tampered["creative"]["reference_images"][0]["committed_key"] = foreign_key
            snapshot.canonical_json = tampered
            s.flush()
            item_id = self._item(s, run_id).id
        self.blob_store.objects[foreign_key] = REF_SIDE
        calls: list[httpx.Request] = []
        adapter = OpenAIImageAdapter(
            base_url="https://openai.test/v1", timeout=5,
            transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)),
        )
        self.assertIsNone(self._dispatch({"openai": adapter}, item_id))
        self.assertEqual(calls, [])
        self.assertNotIn(foreign_key, self.blob_store.reads)
        with tenant_session(self.ctx) as s:
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "failed")
            self.assertIn("reference_not_in_workspace", item.error_summary)


class PlanRecipeTests(PlanFixtures):
    def test_a_plan_run_freezes_its_directions_and_each_item_gets_its_own(self):
        plan = self._ready_plan()
        run = self._run(plan["plan_id"], "sample")
        directions = {d["direction_id"]: d for d in plan["directions"]}

        with tenant_session(self.ctx) as session:
            record = session.get(GenerationRunRecord, run["run_id"])
            snapshot = session.get(GenerationRecipeSnapshotRecord, record.recipe_snapshot_id)
            creative = snapshot.canonical_json["creative"]
            self.assertEqual(creative["test_plan_id"], plan["plan_id"])
            self.assertEqual(set(creative["directions"]), set(directions))
            # Mock Runs freeze the decision too: nothing is sent.
            self.assertFalse(creative["reference_policy"]["use"])
            seen = set()
            for item in session.scalars(
                select(RunItemRecord).where(RunItemRecord.generation_run_id == run["run_id"])
            ):
                recipe = build_creative_recipe(session, self.ctx, item, record)
                self.assertIsNotNone(recipe.direction)
                expected = directions[recipe.direction.direction_key]
                self.assertEqual(recipe.direction.title, expected["title"])
                self.assertEqual(recipe.direction.scene_description, expected["scene_description"])
                self.assertEqual(recipe.direction.headline, expected["copy"]["headline"])
                self.assertEqual(recipe.test_plan_id, plan["plan_id"])
                self.assertEqual(recipe.product.name, "VoltNest 充电桩")
                seen.add(recipe.direction.direction_key)
            self.assertEqual(seen, set(directions))


if __name__ == "__main__":
    unittest.main()
