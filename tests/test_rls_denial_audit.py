"""Cross-workspace isolation and denial-audit tests (Issue #49 Task 10).

Row Level Security is PostgreSQL-only and this repository's test suite runs
on SQLite (see ``tests/test_postgres_rls.py``, gated on
``DOMAIN_OWNER_DATABASE_URL``/``DOMAIN_API_DATABASE_URL``/etc., and
``tests/test_meta_rls_isolation.py`` for the same pattern). RLS coverage for
every Generation -> Assets table (``asset_versions``, ``assets``,
``quality_reviews``, ``generation_runs``, ``generation_drafts``, ``run_items``,
``export_packages``, ``asset_qc_attempts``, plus
``generation_recipe_snapshots``, ``resolution_preview_confirmations``,
``provider_attempt_events``, ``provider_attempt_projections``,
``run_item_asset_version_bindings``, ``asset_qc_check_results`` and
``generation_run_commands``) was already installed by Task 1's
``migrations/versions/n1_generation_assets_expand.py`` (``NEW_WORKSPACE_OWNED``,
picked up by ``tests/test_postgres_rls.py``'s ``all_protected_tables()``) --
there is no new RLS migration to add here.

This file instead does two things, both real on SQLite:

1. Proves the *application*-level workspace boundary every domain function
   in ``app.generation`` already enforces by construction (every query is
   filtered by ``ctx.workspace_id``) -- the same boundary RLS additionally
   enforces at the database layer on PostgreSQL, so a request that reaches
   these functions is denied either way.
2. Proves ``app.platform.denial_audit``'s ``generation.cross_workspace_denial``
   audit event -- new in this task -- is actually emitted and durably
   persisted when a workspace-scoped lookup misses because the id belongs to
   a *different*, real Workspace (not because it was never issued).

Like ``tests/test_asset_download_intent.py`` and every other Task 1-9 test
file in this suite, this uses ``unittest.TestCase`` + ``LibraryFixtures`` +
``tenant_session``, not the plan brief's pytest-flavored
``db_session``/``tenant_ctx``/``other_tenant_ctx``/``make_asset_version``/
``audit_log`` fixtures, none of which exist in this repository.
"""
from __future__ import annotations

from sqlalchemy import select

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, AuditEventRecord, new_id
from app.generation.asset_queries import AssetBlobError, list_asset_versions, resolve_asset_blob_ref
from app.generation.export import ExportError, create_export_package
from app.generation.review import ReviewError, create_review
from app.platform.denial_audit import CROSS_WORKSPACE_DENIAL


class TestRLSDenialAudit(LibraryFixtures):
    """Cross-workspace access is denied, and the denial is auditable."""

    def _make_version(self, ctx, session, **overrides) -> AssetVersionRecord:
        asset_id = self._asset_id(ctx, session)
        fields = dict(
            id=new_id("ver"),
            workspace_id=ctx.workspace_id,
            asset_id=asset_id,
            kind="generated",
            aspect_ratio="1:1",
            language="en",
            object_key=f"legacy/{asset_id}.png",
            content_sha256="c" * 64,
            byte_size=100,
            mime_type="image/png",
        )
        fields.update(overrides)
        version = AssetVersionRecord(**fields)
        session.add(version)
        session.flush()
        return version

    def _audit_events(self, ctx, event_type: str) -> list[AuditEventRecord]:
        """Read back audit events for ``ctx``'s Workspace in a fresh session.

        ``app.platform.denial_audit.record_cross_workspace_denial`` commits
        in its own transaction (see that module's docstring), independent of
        whatever transaction the denied call was running in -- so this must
        also be a fresh read, not a read inside the transaction that raised.
        """
        with tenant_session(ctx) as session:
            return list(
                session.execute(
                    select(AuditEventRecord).where(
                        AuditEventRecord.event_type == event_type,
                        AuditEventRecord.workspace_id == ctx.workspace_id,
                    )
                ).scalars()
            )

    # ---------------------------------------------------------- reads ----

    def test_rls_blocks_cross_workspace_asset_read(self):
        """A gallery listing scoped to another Workspace sees no rows."""
        ctx = self._make_context()
        other_ctx = self._make_context()
        with tenant_session(ctx) as session:
            self._make_version(ctx, session)

        with tenant_session(other_ctx) as other_session:
            result = list_asset_versions(other_session, other_ctx)

        self.assertEqual(result["items"], [])

    # --------------------------------------------------------- review ----

    def test_rls_blocks_cross_workspace_review(self):
        """Reviewing another Workspace's AssetVersion fails closed."""
        ctx = self._make_context()
        other_ctx = self._make_context()
        with tenant_session(ctx) as session:
            version = self._make_version(ctx, session)
            version_id = version.id

        with tenant_session(other_ctx) as other_session:
            with self.assertRaises(ReviewError) as caught:
                create_review(
                    other_session, other_ctx, version_id,
                    "approved", "xws", "actor-evil",
                )
            self.assertEqual(caught.exception.code, "asset_version_not_found")

    # --------------------------------------------------------- export ----

    def test_rls_blocks_cross_workspace_export(self):
        """Exporting another Workspace's AssetVersion fails closed."""
        ctx = self._make_context()
        other_ctx = self._make_context()
        with tenant_session(ctx) as session:
            version = self._make_version(ctx, session)
            version_id = version.id

        with tenant_session(other_ctx) as other_session:
            with self.assertRaises(ExportError) as caught:
                create_export_package(
                    other_session, other_ctx,
                    selector={"version_ids": [version_id]},
                )
            self.assertEqual(caught.exception.code, "asset_version_not_found")

    # --------------------------------------------------- denial audit ----

    def test_denial_audit_emitted_on_cross_workspace(self):
        """A cross-workspace blob resolution attempt emits a structured
        ``generation.cross_workspace_denial`` audit event durably, even
        though the caller's own transaction raises and rolls back."""
        ctx = self._make_context()
        other_ctx = self._make_context()
        with tenant_session(ctx) as session:
            version = self._make_version(
                ctx,
                session,
                committed_object_key="committed/ws1/assets/2026/09/08/v1.png",
                committed_storage_version_id="sv-1",
            )
            version_id = version.id

        with tenant_session(other_ctx) as other_session:
            with self.assertRaises(AssetBlobError):
                resolve_asset_blob_ref(other_session, other_ctx, version_id)

        events = self._audit_events(other_ctx, CROSS_WORKSPACE_DENIAL)
        self.assertGreaterEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.resource_id, version_id)
        self.assertEqual(event.workspace_id, other_ctx.workspace_id)
        self.assertEqual(event.details_json["target_workspace_id"], ctx.workspace_id)
        self.assertNotEqual(event.details_json["target_workspace_id"], other_ctx.workspace_id)

    def test_denial_audit_not_emitted_for_a_never_issued_id(self):
        """A genuinely nonexistent id raises the same error but records
        nothing -- only a real cross-Workspace hit is auditable; a typo'd id
        must not be indistinguishable-from-a-probe in the audit trail
        either, since that would itself leak which ids are real."""
        other_ctx = self._make_context()

        with tenant_session(other_ctx) as other_session:
            with self.assertRaises(AssetBlobError):
                resolve_asset_blob_ref(other_session, other_ctx, "never-issued")

        events = self._audit_events(other_ctx, CROSS_WORKSPACE_DENIAL)
        self.assertEqual(
            [e for e in events if e.resource_id == "never-issued"], []
        )

    def test_denial_audit_emitted_on_cross_workspace_review(self):
        ctx = self._make_context()
        other_ctx = self._make_context()
        with tenant_session(ctx) as session:
            version = self._make_version(ctx, session)
            version_id = version.id

        with tenant_session(other_ctx) as other_session:
            with self.assertRaises(ReviewError):
                create_review(
                    other_session, other_ctx, version_id,
                    "approved", "xws", "actor-evil",
                )

        events = self._audit_events(other_ctx, CROSS_WORKSPACE_DENIAL)
        matching = [e for e in events if e.resource_id == version_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].details_json["target_workspace_id"], ctx.workspace_id)

    def test_denial_audit_emitted_on_cross_workspace_export(self):
        ctx = self._make_context()
        other_ctx = self._make_context()
        with tenant_session(ctx) as session:
            version = self._make_version(ctx, session)
            version_id = version.id

        with tenant_session(other_ctx) as other_session:
            with self.assertRaises(ExportError):
                create_export_package(
                    other_session, other_ctx,
                    selector={"version_ids": [version_id]},
                )

        events = self._audit_events(other_ctx, CROSS_WORKSPACE_DENIAL)
        matching = [e for e in events if e.resource_id == version_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].details_json["target_workspace_id"], ctx.workspace_id)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
