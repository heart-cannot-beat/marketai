"""Template-driven combination fingerprints must not be width-limited.

A three-dimension Dimension Template fingerprints every combination as
``<entry_id>:<dimension_id>=<value_id>`` per dimension; that is well past the
64-character ``run_items.combination_fingerprint`` column PostgreSQL used to
enforce (``StringDataRightTruncation`` on Atomic Submission). SQLite ignores
declared widths, so these checks assert the mapped column types directly and
then drive a real three-dimension submission through the same code path.
"""

from __future__ import annotations

import unittest

from sqlalchemy import Text, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    ExperimentVersionConceptRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    new_id,
    utcnow,
)
from app.generation.models import RunItemRecord
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs


class TestDimensionSignatureWidths(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context()
        # The durable queue is shared by every test in the process, and the
        # Atomic Submission below enqueues 24 generation.run_item Jobs it
        # never processes. Cancel them on the way out so they are not the
        # next thing an unrelated test's Worker claims (test_foundation's
        # Worker tests assert on their own Job being the one processed).
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def test_fingerprint_and_signature_columns_are_unbounded_text(self):
        for column in (
            RunItemRecord.__table__.c.combination_fingerprint,
            RunItemRecord.__table__.c.stable_dimension_signature,
            AssetRecord.__table__.c.combo_id,
            AssetRecord.__table__.c.combo_code,
            ExperimentVersionConceptRecord.__table__.c.dimension_key,
            ExperimentVersionConceptRecord.__table__.c.stable_dimension_signature,
        ):
            self.assertIsInstance(column.type, Text, f"{column.table.name}.{column.name}")

    def test_three_dimension_template_submission_keeps_full_fingerprint(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation

        ctx = self.ctx
        with tenant_session(ctx) as s:
            entry = LibraryEntryRecord(
                id=new_id("lib"),
                workspace_id=ctx.workspace_id,
                entry_type="dimension_template",
                display_name="Charger angles",
                lifecycle="active",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            s.add(entry)
            s.flush()
            version = LibraryVersionRecord(
                id=new_id("libv"),
                workspace_id=ctx.workspace_id,
                entry_id=entry.id,
                entry_type="dimension_template",
                version_no=1,
                content_json={
                    "dimensions": [
                        {"id": "person", "values": [
                            {"id": "with_person", "positive_fragments": ["a person"]},
                            {"id": "no_person", "positive_fragments": ["nobody"]},
                        ]},
                        {"id": "scene", "values": [
                            {"id": "lifestyle", "positive_fragments": ["home"]},
                            {"id": "industrial", "positive_fragments": ["depot"]},
                            {"id": "studio", "positive_fragments": ["studio"]},
                        ]},
                        {"id": "car", "values": [
                            {"id": "visible", "positive_fragments": ["car"]},
                            {"id": "hidden", "positive_fragments": ["no car"]},
                        ]},
                    ],
                },
                content_hash="b" * 64,
                content_schema_version=1,
                lifecycle_revision=1,
                state="effective",
                eligibility="eligible",
                validation_json={},
                origin_command="create",
                published_by=ctx.initiator_id or "test",
                published_at=utcnow(),
            )
            s.add(version)
            s.flush()
            entry.current_version_id = version.id

            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                selectors={"dimension_template_ids": [entry.id]},
                format_config={"variants_per_combination": 2},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            fingerprints = [
                combo["fingerprint"]
                for combo in preview.snapshot_json["recipe"]["selected_combinations"]
            ]
            self.assertEqual(len(fingerprints), 12)
            self.assertTrue(all(len(fp) > 64 for fp in fingerprints), fingerprints[:1])

            result = submit_generation(
                s, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=new_id("ik"),
            )
            items = s.execute(
                select(RunItemRecord)
                .where(
                    RunItemRecord.workspace_id == ctx.workspace_id,
                    RunItemRecord.generation_run_id == result["generation_run_id"],
                )
                .order_by(RunItemRecord.ordinal)
            ).scalars().all()
            self.assertEqual(len(items), 24)
            self.assertEqual(
                sorted({item.combination_fingerprint for item in items}),
                sorted(fingerprints),
            )
            self.assertTrue(
                all(item.stable_dimension_signature == item.combination_fingerprint for item in items)
            )


if __name__ == "__main__":
    unittest.main()
