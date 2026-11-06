"""Gallery thumbnails for generated AssetVersions (Issue #159).

Covers the whole slice:

* ``app.generation.thumbnails`` -- the derivative is a 512px-long-edge,
  EXIF-free JPEG keeping the source aspect ratio, committed with the
  **base64** checksum the Railway Bucket Adapter verifies (known gap "H"),
  under a content-addressed key; an undecodable source yields ``None``
  instead of an error.
* the Worker -- the Mock path (``WorkerRuntime`` against the local
  ``LocalFileBlobStore``) and the BYOK evidence recorder both record the
  derivative on the AssetVersion, and a derivative that cannot be produced
  never fails the RunItem (the original stays the truth).
* ``app.generation.thumbnail_backfill`` -- idempotent, resumable,
  Workspace-scoped, never destructive.
* the list routes -- ``GET /generation/assets`` and
  ``GET /generation/runs/{id}/items`` embed a signed thumbnail link per item
  (``null`` without a derivative), the detail route keeps serving the
  original, and nothing crosses a Workspace boundary.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import tempfile
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from tests.support.library_fixtures import LibraryFixtures, png_bytes
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs

from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.blob_store_railway_bucket import RailwayBucketBlobStore
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    JobRecord,
    new_id,
)
from app.generation import thumbnails
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.thumbnail_backfill import backfill_workspace_thumbnails
from app.generation.thumbnails import (
    THUMBNAIL_LONG_EDGE,
    build_thumbnail,
    commit_thumbnail,
    commit_thumbnail_best_effort,
    thumbnail_committed_key,
    thumbnail_ref,
)
from app.main import app
from app.platform.blob_store import (
    BlobStoreError,
    CommittedBlobRef,
    GeneratedArtifactSpec,
    validate_object_key,
)
from app.platform.http_identity import request_tenant_context
from tests.test_blob_store_railway_bucket import _FakeRailwayClient

#: Header-only PNG: identifies as nothing Pillow can decode.
UNDECODABLE = (
    b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _enabled_logger(name: str) -> logging.Logger:
    """``LibraryFixtures.setUpClass`` migrates through Alembic, whose
    ``env.py`` applies ``fileConfig`` with ``disable_existing_loggers`` --
    every already-imported module logger is switched off for the rest of the
    process. Re-enable the one a test wants to observe with ``assertLogs``."""
    logger = logging.getLogger(name)
    logger.disabled = False
    return logger


def _decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _local_store(root: str) -> LocalFileBlobStore:
    return LocalFileBlobStore(
        root=root, public_base_url="http://blob.test", signing_secret=b"test-secret",
    )


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


class TestBuildThumbnail(LibraryFixtures):
    def test_long_edge_is_bounded_and_aspect_ratio_kept(self):
        landscape = build_thumbnail(png_bytes((1536, 1024)))
        self.assertEqual((landscape.width, landscape.height), (512, 341))
        portrait = build_thumbnail(png_bytes((300, 900)))
        self.assertEqual((portrait.width, portrait.height), (171, 512))
        square = build_thumbnail(png_bytes((1024, 1024)))
        self.assertEqual((square.width, square.height), (512, 512))
        self.assertLess(len(square.content_bytes), len(png_bytes((1024, 1024))))

    def test_encoded_bytes_match_the_recorded_dimensions_and_both_digests(self):
        derivative = build_thumbnail(png_bytes((1024, 768)))
        image = _decode(derivative.content_bytes)
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.size, (derivative.width, derivative.height))
        self.assertEqual(derivative.mime_type, "image/jpeg")
        self.assertEqual(hashlib.sha256(derivative.content_bytes).hexdigest(), derivative.content_sha256)
        self.assertEqual(_b64(derivative.content_bytes), derivative.content_sha256_b64)
        self.assertFalse(image.getexif())

    def test_small_sources_are_never_upscaled(self):
        from app.generation.worker import _mock_png_bytes

        derivative = build_thumbnail(_mock_png_bytes("a" * 64))
        self.assertEqual((derivative.width, derivative.height), (1, 1))
        self.assertEqual(_decode(derivative.content_bytes).size, (1, 1))

    def test_transparency_is_flattened_onto_white(self):
        buffer = io.BytesIO()
        Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(buffer, format="PNG")
        derivative = build_thumbnail(buffer.getvalue())
        image = _decode(derivative.content_bytes).convert("RGB")
        self.assertEqual(image.getpixel((32, 32)), (255, 255, 255))

    def test_undecodable_bytes_raise(self):
        with self.assertRaises(Exception):
            build_thumbnail(UNDECODABLE)
        with self.assertRaises(Exception):
            build_thumbnail(b"not an image at all")

    def test_committed_key_is_content_addressed_and_valid(self):
        key = thumbnail_committed_key("ws_abc-123", "f" * 64, "image/jpeg")
        self.assertEqual(key, f"committed/ws_abc-123/generation_thumbnail/{'f' * 64}.jpg")
        validate_object_key(key)


# ---------------------------------------------------------------------------
# Commit through the storage seam
# ---------------------------------------------------------------------------


class TestCommitThumbnail(LibraryFixtures):
    def test_local_store_commit_records_checksum_size_and_dimensions(self):
        with tempfile.TemporaryDirectory() as root:
            store = _local_store(root)
            committed = asyncio.run(commit_thumbnail(store, "ws_thumb", png_bytes((1024, 1024))))
            stored = Path(root, committed.committed.committed_key).read_bytes()

        self.assertEqual(committed.committed.content_type_verified, "image/jpeg")
        self.assertEqual(committed.mime_type, "image/jpeg")
        self.assertEqual((committed.width, committed.height), (512, 512))
        self.assertEqual(committed.committed.size_bytes, len(stored))
        self.assertEqual(hashlib.sha256(stored).hexdigest(), committed.content_sha256)
        self.assertEqual(committed.committed.checksum_sha256_b64, _b64(stored))
        self.assertEqual(
            committed.committed.committed_key,
            thumbnail_committed_key("ws_thumb", committed.content_sha256, "image/jpeg"),
        )
        self.assertTrue(committed.committed.storage_version_id)

    def test_railway_bucket_adapter_accepts_the_base64_checksum(self):
        """The production Adapter re-hashes the file and refuses a mismatch --
        the hex digest once failed every Mock item (gap "H")."""
        client = _FakeRailwayClient()
        store = RailwayBucketBlobStore(
            bucket="marketai-test", endpoint_url="https://storage.invalid", client=client,
        )
        committed = asyncio.run(commit_thumbnail(store, "ws_rail", png_bytes((1024, 1024))))
        stored = client.objects[committed.committed.committed_key]
        self.assertEqual(hashlib.sha256(stored).hexdigest(), committed.content_sha256)
        self.assertEqual(committed.committed.checksum_sha256_b64, _b64(stored))
        self.assertEqual(client.content_types[committed.committed.committed_key], "image/jpeg")
        self.assertEqual(_decode(stored).size, (512, 512))

    def test_identical_bytes_recommit_under_the_same_key(self):
        with tempfile.TemporaryDirectory() as root:
            store = _local_store(root)
            first = asyncio.run(commit_thumbnail(store, "ws_same", png_bytes((640, 480))))
            second = asyncio.run(commit_thumbnail(store, "ws_same", png_bytes((640, 480))))
        self.assertEqual(first.committed.committed_key, second.committed.committed_key)
        self.assertEqual(first.content_sha256, second.content_sha256)

    def test_best_effort_returns_none_for_undecodable_bytes_and_missing_store(self):
        with tempfile.TemporaryDirectory() as root:
            store = _local_store(root)
            with self.assertLogs(_enabled_logger("app.generation.thumbnails"), level="WARNING") as logs:
                self.assertIsNone(
                    asyncio.run(commit_thumbnail_best_effort(store, "ws_bad", UNDECODABLE))
                )
            self.assertTrue(any("asset_thumbnail_unavailable" in line for line in logs.output))
            self.assertEqual(list(Path(root).rglob("*.jpg")), [])
        self.assertIsNone(asyncio.run(commit_thumbnail_best_effort(None, "ws_bad", png_bytes())))
        self.assertIsNone(asyncio.run(commit_thumbnail_best_effort(store, "ws_bad", None)))

    def test_best_effort_returns_none_when_the_store_refuses_the_commit(self):
        class _RefusingStore:
            async def commit_generated_artifact(self, source, spec):
                raise BlobStoreError("bucket unavailable")

        self.assertIsNone(
            asyncio.run(commit_thumbnail_best_effort(_RefusingStore(), "ws_down", png_bytes()))
        )


# ---------------------------------------------------------------------------
# Worker: Mock path through WorkerRuntime against the local blob store
# ---------------------------------------------------------------------------


class _ThumbnailRefusingStore:
    """Commits originals like the wrapped store, refuses every thumbnail."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def commit_generated_artifact(self, source, spec):
        if "/generation_thumbnail/" in spec.committed_key:
            raise BlobStoreError("thumbnail storage refused")
        return await self._inner.commit_generated_artifact(source, spec)


class TestMockWorkerThumbnails(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def _submit_mock_run(self) -> str:
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation

        with tenant_session(self.ctx) as s:
            draft = create_draft(
                s, self.ctx,
                product_id=self._product_id(self.ctx, s),
                provider="mock", model="mock-v1", execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, self.ctx, draft.id, draft.revision)
            result = submit_generation(
                s, self.ctx,
                preview_id=preview.id, preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision, idempotency_key=new_id("ik"),
            )
        return result["generation_run_id"]

    def _drain(self, runtime) -> None:
        quarantine_foreign_jobs(self.ctx.workspace_id)
        while asyncio.run(runtime.run_once()):
            pass

    def _produced_versions(self, run_id: str) -> list[AssetVersionRecord]:
        with tenant_session(self.ctx) as s:
            items = s.execute(
                select(RunItemRecord).where(
                    RunItemRecord.generation_run_id == run_id,
                    RunItemRecord.workspace_id == self.ctx.workspace_id,
                )
            ).scalars().all()
            self.assertTrue(items)
            versions = []
            for item in items:
                self.assertNotIn(item.state, ("pending", "queued", "failed", "paused"))
                binding = s.execute(
                    select(RunItemAssetVersionBindingRecord).where(
                        RunItemAssetVersionBindingRecord.run_item_id == item.id
                    )
                ).scalar_one()
                versions.append(s.get(AssetVersionRecord, binding.asset_version_id))
            return versions

    def test_mock_run_records_a_verified_thumbnail_beside_the_original(self):
        from app.worker_runtime import WorkerRuntime

        run_id = self._submit_mock_run()
        with tempfile.TemporaryDirectory() as root:
            runtime = WorkerRuntime(owner="thumbnail-worker")
            runtime.blob_store = _local_store(root)
            self._drain(runtime)

            for version in self._produced_versions(run_id):
                self.assertTrue(version.committed_object_key)
                self.assertTrue(version.thumbnail_object_key)
                self.assertNotEqual(version.thumbnail_object_key, version.committed_object_key)
                self.assertTrue(version.thumbnail_storage_version_id)
                self.assertEqual(version.thumbnail_mime_type, "image/jpeg")
                # The Mock original is a 1x1 PNG: the derivative keeps it 1x1.
                self.assertEqual((version.thumbnail_width, version.thumbnail_height), (1, 1))
                stored = Path(root, version.thumbnail_object_key).read_bytes()
                self.assertEqual(len(version.thumbnail_content_sha256), 64)
                self.assertEqual(hashlib.sha256(stored).hexdigest(), version.thumbnail_content_sha256)
                self.assertEqual(version.thumbnail_byte_size, len(stored))
                self.assertEqual(_decode(stored).format, "JPEG")
                # The original is untouched evidence.
                original = Path(root, version.committed_object_key).read_bytes()
                self.assertEqual(hashlib.sha256(original).hexdigest(), version.content_sha256)

            with tenant_session(self.ctx) as s:
                jobs = s.execute(
                    select(JobRecord).where(
                        JobRecord.workspace_id == self.ctx.workspace_id,
                        JobRecord.type == "generation.run_item",
                    )
                ).scalars().all()
            self.assertEqual({(job.state, job.error_code) for job in jobs}, {("succeeded", "")})

    def test_a_refused_thumbnail_never_fails_the_run_item(self):
        from app.worker_runtime import WorkerRuntime

        run_id = self._submit_mock_run()
        with tempfile.TemporaryDirectory() as root:
            runtime = WorkerRuntime(owner="thumbnail-worker-refused")
            runtime.blob_store = _ThumbnailRefusingStore(_local_store(root))
            with self.assertLogs(_enabled_logger("app.generation.thumbnails"), level="WARNING"):
                self._drain(runtime)

            for version in self._produced_versions(run_id):
                self.assertTrue(version.committed_object_key)
                self.assertIsNone(version.thumbnail_object_key)
                self.assertIsNone(version.thumbnail_storage_version_id)
                self.assertIsNone(version.thumbnail_width)
                self.assertIsNone(thumbnail_ref(version))

            with tenant_session(self.ctx) as s:
                jobs = s.execute(
                    select(JobRecord).where(
                        JobRecord.workspace_id == self.ctx.workspace_id,
                        JobRecord.type == "generation.run_item",
                    )
                ).scalars().all()
            self.assertEqual({(job.state, job.error_code) for job in jobs}, {("succeeded", "")})


# ---------------------------------------------------------------------------
# Worker: BYOK evidence recorder
# ---------------------------------------------------------------------------


class TestByokThumbnails(LibraryFixtures):
    def setUp(self) -> None:
        from tests.test_generation_byok import ByokRunChainTests

        self.harness = ByokRunChainTests("test_capabilities_advertise_openai_and_byok_only_with_a_bound_key")
        self.harness.setUp()
        self.ctx = self.harness.ctx
        # Dispatching through the coordinator directly leaves the Run's
        # ``generation.run_item`` Jobs (and the QC Job a success enqueues)
        # open on the shared queue; a later module's ``run_once`` would claim
        # them instead of its own (``tests/support/queue_hygiene``).
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def _dispatch(self, image: bytes):
        from tests.test_generation_byok import _ScriptedAdapter

        self.harness._bind_openai_key()
        with tenant_session(self.ctx) as s:
            run_id, _ = self.harness._submit(s, variants=1)
            item_id = self.harness._items(s, run_id)[0].id
        result = self.harness._dispatch(_ScriptedAdapter(image), item_id)
        self.assertTrue(result.success)
        with tenant_session(self.ctx) as s:
            binding = s.execute(
                select(RunItemAssetVersionBindingRecord).where(
                    RunItemAssetVersionBindingRecord.run_item_id == item_id
                )
            ).scalar_one()
            version = s.get(AssetVersionRecord, binding.asset_version_id)
            self.assertEqual(s.get(RunItemRecord, item_id).state, "qc_pending")
            return version

    def test_a_real_image_gets_a_thumbnail_committed_beside_it(self):
        image = png_bytes((1024, 1024))
        version = self._dispatch(image)
        committed = self.harness.blob_store.committed
        self.assertEqual(len(committed), 2)
        original_spec, original_bytes = committed[0]
        thumbnail_spec, thumbnail_bytes = committed[1]
        self.assertEqual(original_bytes, image)
        self.assertEqual(thumbnail_spec.committed_key, version.thumbnail_object_key)
        self.assertEqual(thumbnail_spec.content_type, "image/jpeg")
        self.assertEqual(thumbnail_spec.expected_sha256_b64, _b64(thumbnail_bytes))
        self.assertEqual(hashlib.sha256(thumbnail_bytes).hexdigest(), version.thumbnail_content_sha256)
        self.assertEqual((version.thumbnail_width, version.thumbnail_height), (512, 512))
        self.assertEqual(version.thumbnail_byte_size, len(thumbnail_bytes))
        self.assertEqual(version.committed_object_key, original_spec.committed_key)
        self.assertEqual(version.execution_mode, "byok")

    def test_an_undecodable_image_still_lands_without_a_thumbnail(self):
        with self.assertLogs(_enabled_logger("app.generation.thumbnails"), level="WARNING"):
            version = self._dispatch(UNDECODABLE)
        self.assertEqual(len(self.harness.blob_store.committed), 1)
        self.assertTrue(version.committed_object_key)
        self.assertIsNone(version.thumbnail_object_key)
        self.assertIsNone(version.thumbnail_content_sha256)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


class TestThumbnailBackfill(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.store = _local_store(self.root.name)

    def _seed_version(self, ctx, session, *, content: bytes | None, seq: int, committed: bool = True) -> str:
        asset_id = self._asset_id(ctx, session)
        fields = dict(
            id=new_id("av"),
            workspace_id=ctx.workspace_id,
            asset_id=asset_id,
            kind="generated",
            aspect_ratio="1:1",
            object_key=f"legacy/{asset_id}.png",
            content_sha256=hashlib.sha256(content or b"").hexdigest(),
            byte_size=len(content or b""),
            mime_type="image/png",
            origin_kind="generation",
            execution_mode="mock",
        )
        if committed:
            key = f"committed/{ctx.workspace_id}/generation_output/{seq}-{new_id('o')}.png"
            if content is not None:
                with tempfile.TemporaryDirectory() as temp_dir:
                    source = Path(temp_dir) / "o.png"
                    source.write_bytes(content)
                    ref = asyncio.run(self.store.commit_generated_artifact(
                        source,
                        GeneratedArtifactSpec(
                            committed_key=key, content_type="image/png",
                            expected_sha256_b64=_b64(content),
                        ),
                    ))
                fields.update(
                    committed_object_key=ref.committed_key,
                    committed_storage_version_id=ref.storage_version_id,
                )
            else:
                # A ref pointing at an object the store does not hold.
                fields.update(committed_object_key=key, committed_storage_version_id="gone")
        version = AssetVersionRecord(**fields)
        session.add(version)
        session.flush()
        return version.id

    def _run(self, ctx, **kwargs):
        return asyncio.run(backfill_workspace_thumbnails(self.store, ctx, **kwargs))

    def _versions(self, ctx, ids):
        with tenant_session(ctx) as s:
            return {vid: s.get(AssetVersionRecord, vid) for vid in ids}

    def test_backfill_is_idempotent_and_skips_rows_without_an_original(self):
        with tenant_session(self.ctx) as s:
            ids = [self._seed_version(self.ctx, s, content=png_bytes((800, 600)), seq=i) for i in range(3)]
            legacy = self._seed_version(self.ctx, s, content=png_bytes(), seq=9, committed=False)

        first = self._run(self.ctx, batch_size=2)
        self.assertEqual((first.examined, first.derived, first.failed), (3, 3, 0))
        self.assertEqual(first.without_original, 1)
        self.assertEqual(first.with_thumbnail_before, 0)

        versions = self._versions(self.ctx, ids + [legacy])
        for vid in ids:
            version = versions[vid]
            self.assertTrue(version.thumbnail_object_key)
            self.assertEqual((version.thumbnail_width, version.thumbnail_height), (512, 384))
            stored = Path(self.root.name, version.thumbnail_object_key).read_bytes()
            self.assertEqual(hashlib.sha256(stored).hexdigest(), version.thumbnail_content_sha256)
            self.assertEqual(version.thumbnail_byte_size, len(stored))
            self.assertIsNotNone(thumbnail_ref(version))
        self.assertIsNone(versions[legacy].thumbnail_object_key)
        keys_after_first = {versions[vid].thumbnail_object_key for vid in ids}

        second = self._run(self.ctx)
        self.assertEqual((second.examined, second.derived, second.failed), (0, 0, 0))
        self.assertEqual(second.with_thumbnail_before, 3)
        self.assertEqual(
            {v.thumbnail_object_key for v in self._versions(self.ctx, ids).values()},
            keys_after_first,
        )

    def test_limit_resumes_where_the_previous_pass_stopped(self):
        with tenant_session(self.ctx) as s:
            ids = [self._seed_version(self.ctx, s, content=png_bytes((640, 640)), seq=i) for i in range(5)]

        first = self._run(self.ctx, limit=2, batch_size=10)
        self.assertEqual((first.examined, first.derived), (2, 2))
        done = [vid for vid, v in self._versions(self.ctx, ids).items() if v.thumbnail_object_key]
        self.assertEqual(len(done), 2)

        second = self._run(self.ctx, limit=10, batch_size=2)
        self.assertEqual((second.examined, second.derived), (3, 3))
        self.assertEqual(second.with_thumbnail_before, 2)
        self.assertTrue(all(v.thumbnail_object_key for v in self._versions(self.ctx, ids).values()))

    def test_dry_run_counts_without_writing(self):
        with tenant_session(self.ctx) as s:
            ids = [self._seed_version(self.ctx, s, content=png_bytes(), seq=i) for i in range(2)]
        report = self._run(self.ctx, dry_run=True)
        self.assertEqual((report.examined, report.derived), (2, 0))
        self.assertTrue(report.dry_run)
        self.assertTrue(all(v.thumbnail_object_key is None for v in self._versions(self.ctx, ids).values()))
        self.assertEqual(list(Path(self.root.name).rglob("*.jpg")), [])

    def test_failures_are_counted_left_null_and_do_not_stall_the_pass(self):
        with tenant_session(self.ctx) as s:
            missing = self._seed_version(self.ctx, s, content=None, seq=0)
            undecodable = self._seed_version(self.ctx, s, content=UNDECODABLE, seq=1)
            good = self._seed_version(self.ctx, s, content=png_bytes(), seq=2)

        with self.assertLogs(_enabled_logger("app.generation.thumbnail_backfill"), level="WARNING"):
            report = self._run(self.ctx, batch_size=1)
        self.assertEqual((report.examined, report.derived, report.failed), (3, 1, 2))
        self.assertEqual(
            sorted(f["asset_version_id"] for f in report.failures), sorted([missing, undecodable])
        )
        versions = self._versions(self.ctx, [missing, undecodable, good])
        self.assertIsNone(versions[missing].thumbnail_object_key)
        self.assertIsNone(versions[undecodable].thumbnail_object_key)
        self.assertTrue(versions[good].thumbnail_object_key)
        # Originals are never touched.
        self.assertTrue(versions[undecodable].committed_object_key)

        # The next run retries exactly the two that stayed null.
        with self.assertLogs(_enabled_logger("app.generation.thumbnail_backfill"), level="WARNING"):
            again = self._run(self.ctx)
        self.assertEqual((again.examined, again.derived, again.failed), (2, 0, 2))

    def test_backfill_never_crosses_a_workspace(self):
        other = self._make_context()
        with tenant_session(self.ctx) as s:
            mine = self._seed_version(self.ctx, s, content=png_bytes(), seq=0)
        with tenant_session(other) as s:
            theirs = self._seed_version(other, s, content=png_bytes(), seq=0)

        report = self._run(self.ctx)
        self.assertEqual((report.examined, report.derived), (1, 1))
        self.assertTrue(self._versions(self.ctx, [mine])[mine].thumbnail_object_key)
        self.assertIsNone(self._versions(other, [theirs])[theirs].thumbnail_object_key)
        self.assertIn(f"/{self.ctx.workspace_id}/", self._versions(self.ctx, [mine])[mine].thumbnail_object_key)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestThumbnailLinksOnListRoutes(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _seed(self, ctx, session, *, with_thumbnail: bool) -> AssetVersionRecord:
        product_id = self._product_id(ctx, session)
        run = GenerationRunRecord(
            id=new_id("gr"), workspace_id=ctx.workspace_id, product_id=product_id,
            plan_id=self._plan_id(ctx, session), provider="mock", model="mock-v1",
            aspect_ratio="1:1", image_size="1024x1024", variants=1, use_ref=False,
            status="succeeded", total=1, done=1, failed=0,
            est_cost=Decimal("0"), currency="USD", revision=1,
        )
        session.add(run)
        session.flush()
        asset = AssetRecord(
            id=new_id("asset"), workspace_id=ctx.workspace_id, product_id=product_id,
            run_id=run.id, combo_id="c1", combo_code="c1", tags_json={"scene": "studio"},
            filename="x.png", provider="mock", model="mock-v1", prompt="", fingerprint=new_id("fp"),
            status="succeeded",
        )
        session.add(asset)
        session.flush()
        fields = dict(
            id=new_id("av"), workspace_id=ctx.workspace_id, asset_id=asset.id, kind="generated",
            aspect_ratio="1:1", object_key="legacy/x.png", content_sha256="c" * 64, byte_size=10,
            mime_type="image/png", origin_kind="generation", execution_mode="mock",
            committed_object_key=f"committed/{ctx.workspace_id}/generation_output/{'c' * 64}.png",
            committed_storage_version_id="orig-v1",
        )
        if with_thumbnail:
            fields.update(
                thumbnail_object_key=f"committed/{ctx.workspace_id}/generation_thumbnail/{'d' * 64}.jpg",
                thumbnail_storage_version_id="thumb-v1",
                thumbnail_content_sha256="d" * 64,
                thumbnail_byte_size=1234,
                thumbnail_mime_type="image/jpeg",
                thumbnail_width=512,
                thumbnail_height=384,
            )
        version = AssetVersionRecord(**fields)
        session.add(version)
        session.flush()
        item = RunItemRecord(
            id=new_id("ri"), workspace_id=ctx.workspace_id, generation_run_id=run.id,
            ordinal=0, state="succeeded", combination_fingerprint="c1", variant_index=0,
            tags_json={"scene": "studio"}, execution_mode="mock", semantic_fingerprint=new_id("sf"),
            revision=1,
        )
        session.add(item)
        session.flush()
        session.add(RunItemAssetVersionBindingRecord(
            id=new_id("riavb"), workspace_id=ctx.workspace_id, run_item_id=item.id,
            asset_version_id=version.id, kind="produced",
        ))
        session.flush()
        return version

    def test_gallery_items_carry_a_signed_thumbnail_link_or_null(self):
        with tenant_session(self.ctx) as s:
            with_thumb = self._seed(self.ctx, s, with_thumbnail=True)
            without = self._seed(self.ctx, s, with_thumbnail=False)
            with_thumb_id, without_id = with_thumb.id, without.id

        response = self.client.get("/api/v1/generation/assets")
        self.assertEqual(response.status_code, 200, response.text)
        by_id = {item["id"]: item for item in response.json()["items"]}
        link = by_id[with_thumb_id]["thumbnail"]
        self.assertIsNotNone(link)
        self.assertIn(f"generation_thumbnail/{'d' * 64}.jpg", link["url"])
        self.assertEqual(link["mime_type"], "image/jpeg")
        self.assertEqual((link["width"], link["height"]), (512, 384))
        self.assertTrue(link["expires_at"])
        # The link is for the derivative, never the original.
        self.assertNotIn("generation_output", link["url"])
        self.assertIsNone(by_id[without_id]["thumbnail"])

    def test_run_items_carry_the_bound_version_and_its_thumbnail_link(self):
        with tenant_session(self.ctx) as s:
            version = self._seed(self.ctx, s, with_thumbnail=True)
            version_id, run_id = version.id, s.get(AssetRecord, version.asset_id).run_id

        response = self.client.get(f"/api/v1/generation/runs/{run_id}/items")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["bound_asset_version_id"], version_id)
        self.assertEqual(items[0]["tags"], {"scene": "studio"})
        self.assertIn(f"generation_thumbnail/{'d' * 64}.jpg", items[0]["thumbnail"]["url"])
        self.assertEqual(items[0]["thumbnail"]["width"], 512)

        detail = self.client.get(f"/api/v1/generation/runs/{run_id}/items/{items[0]['id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["bound_asset_version_id"], version_id)

    def test_run_item_tags_fall_back_to_the_frozen_combination_values(self):
        """Atomic Submission leaves ``tags_json`` empty; the tile labels come
        from the frozen combination identity, with template entry ids cut
        off the dimension keys unless that would make two keys collide."""
        from app.generation.run_queries import _combination_value_tags

        def item(**overrides) -> RunItemRecord:
            fields = dict(
                id=new_id("ri"), workspace_id=self.ctx.workspace_id, generation_run_id="gr_x",
                ordinal=0, state="succeeded", combination_fingerprint="fp", variant_index=0,
                tags_json={}, combination_identity_json={}, execution_mode="mock",
                semantic_fingerprint="sf", revision=1,
            )
            fields.update(overrides)
            return RunItemRecord(**fields)

        template = item(combination_identity_json={
            "fingerprint": "lib_a:scene=studio:lib_a:person=with_person",
            "dimensions": {"lib_a:scene": "studio", "lib_a:person": "with_person"},
            "concepts": [],
        })
        self.assertEqual(
            _combination_value_tags(template), {"scene": "studio", "person": "with_person"},
        )
        scalar = item(combination_identity_json={"dimensions": {"palette": "warm"}})
        self.assertEqual(_combination_value_tags(scalar), {"palette": "warm"})
        colliding = item(combination_identity_json={
            "dimensions": {"lib_a:scene": "studio", "lib_b:scene": "street"},
        })
        self.assertEqual(
            _combination_value_tags(colliding), {"lib_a:scene": "studio", "lib_b:scene": "street"},
        )
        explicit = item(tags_json={"mood": "calm"}, combination_identity_json={"dimensions": {"x": "y"}})
        self.assertEqual(_combination_value_tags(explicit), {"mood": "calm"})
        self.assertEqual(_combination_value_tags(item(combination_identity_json={"fingerprint": "default", "dimensions": {}})), {})

    def test_run_items_without_a_produced_version_have_no_link(self):
        with tenant_session(self.ctx) as s:
            version = self._seed(self.ctx, s, with_thumbnail=False)
            run_id = s.get(AssetRecord, version.asset_id).run_id
            s.add(RunItemRecord(
                id=new_id("ri"), workspace_id=self.ctx.workspace_id, generation_run_id=run_id,
                ordinal=1, state="running", combination_fingerprint="c2", variant_index=0,
                tags_json={}, execution_mode="mock", semantic_fingerprint=new_id("sf"), revision=1,
            ))

        response = self.client.get(f"/api/v1/generation/runs/{run_id}/items")
        self.assertEqual(response.status_code, 200, response.text)
        by_ordinal = {item["ordinal"]: item for item in response.json()["items"]}
        self.assertIsNone(by_ordinal[0]["thumbnail"])  # bound, but no derivative yet
        self.assertIsNotNone(by_ordinal[0]["bound_asset_version_id"])
        self.assertIsNone(by_ordinal[1]["thumbnail"])  # nothing produced
        self.assertIsNone(by_ordinal[1]["bound_asset_version_id"])

    def test_asset_detail_keeps_the_original_and_no_foreign_link_is_signed(self):
        other = self._make_context()
        with tenant_session(other) as s:
            theirs = self._seed(other, s, with_thumbnail=True)
            theirs_id, their_run = theirs.id, s.get(AssetRecord, theirs.asset_id).run_id

        # The caller's Workspace sees nothing of the other Workspace's gallery
        # or Run, with or without thumbnails.
        gallery = self.client.get("/api/v1/generation/assets")
        self.assertEqual(gallery.status_code, 200)
        self.assertNotIn(theirs_id, {item["id"] for item in gallery.json()["items"]})
        self.assertEqual(self.client.get(f"/api/v1/generation/runs/{their_run}/items").status_code, 404)

        # And the refs helper itself resolves nothing for a foreign id.
        with tenant_session(self.ctx) as s:
            self.assertEqual(thumbnails.thumbnail_refs_for_versions(s, self.ctx, [theirs_id]), {})

        # The detail projection does not grow a thumbnail: Asset Detail and
        # the Lightbox keep fetching the original through the download intent.
        with tenant_session(self.ctx) as s:
            mine = self._seed(self.ctx, s, with_thumbnail=True)
            mine_id = mine.id
        detail = self.client.get(f"/api/v1/generation/asset-versions/{mine_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertNotIn("thumbnail", detail.json())
        self.assertEqual(detail.json()["content_sha256"], "c" * 64)
