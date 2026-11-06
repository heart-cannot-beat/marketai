"""Tests for Gate 0.4 — Command impact projections (Publish, Withdraw, Archive).

Covers:
- Preview impact for each command kind
- Impact verification: happy path, stale, expired, consumed, wrong-command,
  wrong-actor, wrong-workspace, wrong-revision, mismatched-hash
- Impact-verified publish, withdraw, and archive end-to-end
- Withdraw/archive downstream effects on dependents
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LibraryCommandImpactRecord, utcnow
from app.library import command_impacts, service
from app.library.command_impacts import (
    ImpactVerificationError,
    preview_archive_impact,
    preview_publish_impact,
    preview_withdraw_impact,
)
from app.library.contracts import ENTRY_PERSON, ENTRY_PRODUCT, ROLE_OWNER
from app.library.service import LibraryConflict


class PublishImpactTests(LibraryFixtures):

    def _publishable_entry(self, ctx):
        ref_id, ref_ver = self._reference_version(ctx)
        content = self._person_content(ref_ver["id"])
        entry = service.create_entry(ctx, entry_type=ENTRY_PERSON, display_name="冲击人物", content=content)
        return entry, ref_ver

    def test_preview_publish_returns_projection_with_impact_id_and_hash(self):
        ctx = self.owner_ctx(self.ctx())
        entry, _ = self._publishable_entry(ctx)
        result = preview_publish_impact(ctx, entry["id"])

        self.assertIn("impact_id", result)
        self.assertIn("impact_hash", result)
        self.assertEqual(result["command_kind"], "publish")
        self.assertEqual(result["target_entry_id"], entry["id"])
        self.assertEqual(result["target_revision"], entry["draft"]["revision"])
        self.assertIn("projection", result)
        proj = result["projection"]
        self.assertEqual(proj["command_kind"], "publish")
        self.assertIn("content_hash", proj)
        self.assertIn("validation_ok", proj)
        self.assertIn("blocking", proj)
        self.assertIn("warnings", proj)

    def test_preview_publish_reports_invalid_must_match_shape_without_crashing(self):
        ctx = self.owner_ctx(self.ctx())
        entry = service.create_entry(
            ctx,
            entry_type=ENTRY_PRODUCT,
            display_name="Invalid must-match shape",
            content={
                "name": "Product",
                "description": "Description",
                "audience": "Audience",
                "must_match": True,
            },
        )

        result = preview_publish_impact(ctx, entry["id"])

        projection = result["projection"]
        self.assertFalse(projection["validation_ok"])
        self.assertIn(
            "must_match_shape",
            {finding["code"] for finding in projection["blocking"]},
        )

    def test_preview_publish_on_archived_entry_raises(self):
        ctx = self.owner_ctx(self.ctx())
        entry_id, version = self._publish(
            ctx, ENTRY_PERSON, "归档测试", self._person_content(self._reference_version(ctx)[1]["id"]),
        )
        entry = service.get_entry(ctx, entry_id)
        service.archive_entry(ctx, entry_id, expected_revision=entry["draft"]["revision"])
        with self.assertRaises(LibraryConflict):
            preview_publish_impact(ctx, entry_id)

    def test_publish_with_verified_impact_succeeds(self):
        ctx = self.owner_ctx(self.ctx())
        entry, _ = self._publishable_entry(ctx)
        entry_id = entry["id"]
        revision = entry["draft"]["revision"]

        impact = preview_publish_impact(ctx, entry_id)
        version = service.publish(
            ctx, entry_id, revision,
            impact_id=impact["impact_id"],
            impact_hash=impact["impact_hash"],
        )
        self.assertIn("id", version)
        self.assertEqual(version["entry_id"], entry_id)

    def test_previewing_the_same_unchanged_draft_twice_mints_two_tokens(self):
        """The dialog opened twice, or a retry after a failed command.

        Both previews project the identical draft, so a hash of the
        projection alone collided on ``(workspace_id, impact_hash)`` and the
        second preview failed with ``UniqueViolation`` (500 in production).
        Each preview is its own single-use token now; the latest one still
        verifies and publishes.
        """
        ctx = self.owner_ctx(self.ctx())
        entry, _ = self._publishable_entry(ctx)
        entry_id = entry["id"]
        revision = entry["draft"]["revision"]

        first = preview_publish_impact(ctx, entry_id)
        second = preview_publish_impact(ctx, entry_id)

        self.assertEqual(first["projection"], second["projection"])
        self.assertNotEqual(first["impact_id"], second["impact_id"])
        self.assertNotEqual(first["impact_hash"], second["impact_hash"])

        version = service.publish(
            ctx, entry_id, revision,
            impact_id=second["impact_id"],
            impact_hash=second["impact_hash"],
        )
        self.assertEqual(version["entry_id"], entry_id)

        # The earlier token is bound to its own record: it is neither
        # consumed by the publish above nor usable with the newer hash.
        with tenant_session(ctx) as session:
            self.assertEqual(
                session.get(LibraryCommandImpactRecord, first["impact_id"]).consumption_state,
                "available",
            )
            self.assertEqual(
                session.get(LibraryCommandImpactRecord, second["impact_id"]).consumption_state,
                "consumed",
            )

    def test_a_fresh_preview_after_an_expired_one_is_issued(self):
        ctx = self.owner_ctx(self.ctx())
        entry, _ = self._publishable_entry(ctx)
        entry_id = entry["id"]

        stale = preview_publish_impact(ctx, entry_id)
        with tenant_session(ctx) as session:
            record = session.get(LibraryCommandImpactRecord, stale["impact_id"])
            record.expires_at = utcnow() - timedelta(seconds=1)
            session.flush()

        fresh = preview_publish_impact(ctx, entry_id)
        self.assertNotEqual(fresh["impact_hash"], stale["impact_hash"])
        version = service.publish(
            ctx, entry_id, entry["draft"]["revision"],
            impact_id=fresh["impact_id"],
            impact_hash=fresh["impact_hash"],
        )
        self.assertEqual(version["entry_id"], entry_id)

    def test_publish_with_wrong_impact_hash_raises(self):
        ctx = self.owner_ctx(self.ctx())
        entry, _ = self._publishable_entry(ctx)
        entry_id = entry["id"]
        revision = entry["draft"]["revision"]

        impact = preview_publish_impact(ctx, entry_id)
        with self.assertRaises(LibraryConflict) as cm:
            service.publish(
                ctx, entry_id, revision,
                impact_id=impact["impact_id"],
                impact_hash="wrong_hash_value",
            )
        self.assertIn("impact_hash_mismatch", str(cm.exception.detail))


class WithdrawImpactTests(LibraryFixtures):

    def test_preview_withdraw_returns_projection(self):
        ctx = self.owner_ctx(self.ctx())
        ref_id, ref_ver = self._reference_version(ctx)
        entry_id, version = self._publish(
            ctx, ENTRY_PERSON, "撤回测试", self._person_content(ref_ver["id"]),
        )
        result = preview_withdraw_impact(ctx, version["id"])

        self.assertIn("impact_id", result)
        self.assertIn("impact_hash", result)
        self.assertEqual(result["command_kind"], "withdraw")
        self.assertEqual(result["target_version_id"], version["id"])
        proj = result["projection"]
        self.assertIn("dependents", proj)
        self.assertIn("is_current_effective", proj)
        self.assertTrue(proj["is_current_effective"])

    def test_preview_withdraw_already_withdrawn_raises(self):
        ctx = self.owner_ctx(self.ctx())
        ref_id, ref_ver = self._reference_version(ctx)
        entry_id, version = self._publish(
            ctx, ENTRY_PERSON, "已撤回", self._person_content(ref_ver["id"]),
        )
        service.withdraw_version(ctx, version["id"], reason="测试撤回")
        with self.assertRaises(LibraryConflict):
            preview_withdraw_impact(ctx, version["id"])

    def test_withdraw_with_verified_impact_succeeds(self):
        ctx = self.owner_ctx(self.ctx())
        ref_id, ref_ver = self._reference_version(ctx)
        entry_id, version = self._publish(
            ctx, ENTRY_PERSON, "撤回验证", self._person_content(ref_ver["id"]),
        )
        impact = preview_withdraw_impact(ctx, version["id"])
        result = service.withdraw_version(
            ctx, version["id"], reason="测试撤回",
            impact_id=impact["impact_id"],
            impact_hash=impact["impact_hash"],
        )
        self.assertEqual(result["version_id"], version["id"])

    def test_withdraw_with_dependents_shows_downstream(self):
        ctx = self.owner_ctx(self.ctx())
        ref_id, ref_ver = self._reference_version(ctx)
        group_id, group_ver = self._group_version(ctx, ref_ver["id"])
        product_content = self._product_content(ref_ver["id"], group_ver["id"])
        product_id, product_ver = self._publish(ctx, "product", "产品", product_content)

        impact = preview_withdraw_impact(ctx, ref_ver["id"])
        proj = impact["projection"]
        self.assertGreater(proj["dependent_count"], 0)


class ArchiveImpactTests(LibraryFixtures):

    def test_preview_archive_returns_projection(self):
        ctx = self.owner_ctx(self.ctx())
        ref_id, ref_ver = self._reference_version(ctx)
        content = self._person_content(ref_ver["id"])
        entry = service.create_entry(ctx, entry_type=ENTRY_PERSON, display_name="归档预览", content=content)
        result = preview_archive_impact(ctx, entry["id"])

        self.assertIn("impact_id", result)
        self.assertIn("impact_hash", result)
        self.assertEqual(result["command_kind"], "archive")
        self.assertEqual(result["target_entry_id"], entry["id"])
        proj = result["projection"]
        self.assertIn("draft_revision", proj)
        self.assertIn("versions", proj)

    def test_preview_archive_already_archived_raises(self):
        ctx = self.owner_ctx(self.ctx())
        ref_id, ref_ver = self._reference_version(ctx)
        entry_id, version = self._publish(
            ctx, ENTRY_PERSON, "已归档", self._person_content(ref_ver["id"]),
        )
        entry = service.get_entry(ctx, entry_id)
        service.archive_entry(ctx, entry_id, expected_revision=entry["draft"]["revision"])
        with self.assertRaises(LibraryConflict):
            preview_archive_impact(ctx, entry_id)

    def test_archive_with_verified_impact_succeeds(self):
        ctx = self.owner_ctx(self.ctx())
        ref_id, ref_ver = self._reference_version(ctx)
        content = self._person_content(ref_ver["id"])
        entry = service.create_entry(ctx, entry_type=ENTRY_PERSON, display_name="归档验证", content=content)
        entry_id = entry["id"]
        revision = entry["draft"]["revision"]

        impact = preview_archive_impact(ctx, entry_id)
        result = service.archive_entry(
            ctx, entry_id, expected_revision=revision,
            impact_id=impact["impact_id"],
            impact_hash=impact["impact_hash"],
        )
        self.assertEqual(result["lifecycle"], "archived")


class ImpactVerificationTests(LibraryFixtures):

    def _make_impact(self, ctx):
        ref_id, ref_ver = self._reference_version(ctx)
        content = self._person_content(ref_ver["id"])
        entry = service.create_entry(ctx, entry_type=ENTRY_PERSON, display_name="验证测试", content=content)
        return entry, preview_publish_impact(ctx, entry["id"])

    def test_consumed_impact_cannot_be_reused(self):
        ctx = self.owner_ctx(self.ctx())
        entry, impact = self._make_impact(ctx)
        entry_id = entry["id"]
        revision = entry["draft"]["revision"]

        service.publish(
            ctx, entry_id, revision,
            impact_id=impact["impact_id"],
            impact_hash=impact["impact_hash"],
        )

        entry2 = service.get_entry(ctx, entry_id)
        with self.assertRaises(LibraryConflict) as cm:
            service.publish(
                ctx, entry_id, entry2["draft"]["revision"],
                impact_id=impact["impact_id"],
                impact_hash=impact["impact_hash"],
            )
        self.assertIn("impact_consumed", str(cm.exception.detail))

    def test_expired_impact_rejected(self):
        ctx = self.owner_ctx(self.ctx())
        entry, impact = self._make_impact(ctx)

        with tenant_session(ctx) as session:
            record = session.get(LibraryCommandImpactRecord, impact["impact_id"])
            record.expires_at = utcnow() - timedelta(seconds=1)
            session.flush()

        with self.assertRaises(LibraryConflict) as cm:
            service.publish(
                ctx, entry["id"], entry["draft"]["revision"],
                impact_id=impact["impact_id"],
                impact_hash=impact["impact_hash"],
            )
        self.assertIn("impact_expired", str(cm.exception.detail))

    def test_wrong_command_kind_rejected(self):
        ctx = self.owner_ctx(self.ctx())
        entry, impact = self._make_impact(ctx)

        with self.assertRaises(LibraryConflict) as cm:
            service.archive_entry(
                ctx, entry["id"], expected_revision=entry["draft"]["revision"],
                impact_id=impact["impact_id"],
                impact_hash=impact["impact_hash"],
            )
        self.assertIn("impact_wrong_command", str(cm.exception.detail))

    def test_wrong_actor_rejected(self):
        ctx = self.owner_ctx(self.ctx())
        entry, impact = self._make_impact(ctx)

        other_ctx = self.owner_ctx(ctx, actor="other_user")
        with self.assertRaises(LibraryConflict) as cm:
            service.publish(
                other_ctx, entry["id"], entry["draft"]["revision"],
                impact_id=impact["impact_id"],
                impact_hash=impact["impact_hash"],
            )
        self.assertIn("impact_wrong_actor", str(cm.exception.detail))

    def test_wrong_entry_rejected(self):
        ctx = self.owner_ctx(self.ctx())
        entry, impact = self._make_impact(ctx)

        ref_id2, ref_ver2 = self._reference_version(ctx)
        content2 = self._person_content(ref_ver2["id"])
        entry2 = service.create_entry(ctx, entry_type=ENTRY_PERSON, display_name="其他条目", content=content2)
        with self.assertRaises(LibraryConflict) as cm:
            service.publish(
                ctx, entry2["id"], entry2["draft"]["revision"],
                impact_id=impact["impact_id"],
                impact_hash=impact["impact_hash"],
            )
        self.assertIn("impact_wrong_target", str(cm.exception.detail))

    def test_cross_workspace_rejected(self):
        ctx1 = self.owner_ctx(self.ctx())
        entry, impact = self._make_impact(ctx1)

        ctx2 = self.owner_ctx(self.ctx())
        ref_id2, ref_ver2 = self._reference_version(ctx2)
        content2 = self._person_content(ref_ver2["id"])
        entry2 = service.create_entry(ctx2, entry_type=ENTRY_PERSON, display_name="跨租户", content=content2)
        with self.assertRaises(LibraryConflict) as cm:
            service.publish(
                ctx2, entry2["id"], entry2["draft"]["revision"],
                impact_id=impact["impact_id"],
                impact_hash=impact["impact_hash"],
            )
        self.assertIn("impact_not_found", str(cm.exception.detail))

    def test_publish_without_impact_still_works(self):
        ctx = self.owner_ctx(self.ctx())
        ref_id, ref_ver = self._reference_version(ctx)
        content = self._person_content(ref_ver["id"])
        entry = service.create_entry(ctx, entry_type=ENTRY_PERSON, display_name="无冲击", content=content)
        version = service.publish(ctx, entry["id"], entry["draft"]["revision"])
        self.assertIn("id", version)


if __name__ == "__main__":
    unittest.main()
