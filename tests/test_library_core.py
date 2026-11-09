"""创意库核心自动化证据（Issue #19）。

覆盖 ticket 要求的四类证据：
1. 每种条目类型的状态机与不可变性；
2. 租户隔离 / 角色 / 参考权利负向测试；
3. quarantine → 受控派生（使用安全 fixture）；
4. Withdraw 传播与并发 expected-revision。
"""
from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# tests.support.library_fixtures sets the MARKETAI_* env vars (data dir,
# database URL, default workspace) via os.environ.setdefault as a side effect
# of import -- it must be imported before *any* `app.*` module, since
# app.config reads those vars at import time and binds them once. Importing
# app.config first would silently point every test in this file at whatever
# default/shared database the process picks up instead of this file's own
# isolated temp database.
from tests.support.library_fixtures import (  # noqa: E402
    FUTURE,
    PAST,
    LibraryFixtures,
    jpeg_with_exif,
    png_bytes,
)

from PIL import Image  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.exc import IntegrityError, OperationalError  # noqa: E402

from app import config  # noqa: E402
from app.adapters.persistence.database import platform_session, tenant_session  # noqa: E402
from app.platform.tenancy import TenantContext  # noqa: E402
from app.adapters.persistence.models import (  # noqa: E402
    LibraryAuditRecord,
    LibraryDraftRecord,
    LibraryIdempotencyRecord,
    LibraryVersionRecord,
    ProductRecord,
    ReferenceObjectRecord,
    WorkspaceRecord,
)
from app.library import references, service  # noqa: E402
from app.library.contracts import (  # noqa: E402
    DRAFT_EDITING,
    DRAFT_READY,
    DRAFT_VALIDATION_FAILED,
    ELIGIBILITY_BLOCKED,
    ELIGIBILITY_ELIGIBLE,
    ENTRY_DATA_SOURCE,
    ENTRY_MARKET_MAPPING,
    ENTRY_OFFER,
    ENTRY_PERSON,
    ENTRY_PRODUCT,
    ENTRY_REFERENCE_ASSET,
    ENTRY_REFERENCE_GROUP,
    ENTRY_SELECTION_RULE,
    LIFECYCLE_ARCHIVED,
    ROLE_MEMBER,
    ROLE_OWNER,
    VERSION_EFFECTIVE,
    VERSION_SUPERSEDED,
    VERSION_WITHDRAWN,
    WITHDRAWAL_DOWNSTREAM_EVENT_TYPES,
    validate_content,
)


class LibraryCoreTests(LibraryFixtures):
    # ------------------------------------------------------------------
    # 1. 状态机与不可变性
    # ------------------------------------------------------------------

    def test_product_entry_creates_downstream_identity_bridge(self) -> None:
        ctx = self.ctx()
        entry = service.create_entry(
            ctx,
            entry_type=ENTRY_PRODUCT,
            display_name="Bridge Product",
        )

        with tenant_session(ctx) as session:
            product = session.scalar(
                select(ProductRecord).where(
                    ProductRecord.workspace_id == ctx.workspace_id,
                    ProductRecord.product_profile_entry_id == entry["id"],
                )
            )
            self.assertIsNotNone(product)
            self.assertEqual(product.name, "Bridge Product")

    def test_every_entry_type_publishes_an_immutable_version_through_the_draft_state_machine(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        expected_types = {
            "reference": ENTRY_REFERENCE_ASSET,
            "group": ENTRY_REFERENCE_GROUP,
            "rule": ENTRY_SELECTION_RULE,
            "product": ENTRY_PRODUCT,
            "offer": ENTRY_OFFER,
            # the stack's mapping stays a draft (its data source ID is opaque); mapping
            # publish is covered by test_market_source_mapping_roundtrip and test_library_market_mapping.py
            "person": ENTRY_PERSON,
        }
        for key, entry_type in expected_types.items():
            entry_id, version = stack[key]
            with self.subTest(entry_type=entry_type):
                self.assertEqual(version["entry_type"], entry_type)
                self.assertEqual(version["version_no"], 1)
                self.assertEqual(version["state"], VERSION_EFFECTIVE)
                self.assertEqual(version["eligibility"], ELIGIBILITY_ELIGIBLE)
                entry = service.get_entry(ctx, entry_id)
                self.assertEqual(entry["current_version_id"], version["id"])
                # 发布后 Draft 重新以新版本为基准，继续编辑不影响已发布内容。
                self.assertEqual(entry["draft"]["base_version_id"], version["id"])
                self.assertEqual(entry["draft"]["state"], DRAFT_EDITING)

    def test_editing_after_publish_never_rewrites_the_published_version(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        product_entry, product_v1 = stack["product"]
        frozen_content = dict(product_v1["content"])
        frozen_hash = product_v1["content_hash"]

        entry = service.get_entry(ctx, product_entry)
        edited = dict(entry["draft"]["content"])
        edited["description"] = "改写后的描述"
        service.save_draft(ctx, product_entry, edited, entry["draft"]["revision"])

        after = service.get_version(ctx, product_v1["id"])
        self.assertEqual(after["content"], frozen_content)
        self.assertEqual(after["content_hash"], frozen_hash)
        self.assertEqual(after["state"], VERSION_EFFECTIVE)

    def test_product_semantic_change_creates_a_new_version_and_supersedes_the_old_one(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        product_entry, v1 = stack["product"]

        entry = service.get_entry(ctx, product_entry)
        edited = dict(entry["draft"]["content"])
        edited["audience"] = "学生人群"
        draft = service.save_draft(ctx, product_entry, edited, entry["draft"]["revision"])
        report = service.validate_draft(ctx, product_entry, expected_revision=draft["revision"])
        self.assertTrue(report["ok"], report["blocking"])
        v2 = service.publish(ctx, product_entry, draft["revision"])

        self.assertEqual(v2["version_no"], 2)
        self.assertNotEqual(v2["content_hash"], v1["content_hash"])
        self.assertEqual(service.get_version(ctx, v1["id"])["state"], VERSION_SUPERSEDED)
        self.assertEqual(service.get_version(ctx, v1["id"])["content"]["audience"], "差旅办公人群")
        self.assertEqual(service.get_entry(ctx, product_entry)["current_version_id"], v2["id"])

    def test_republishing_identical_content_is_refused(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        product_entry, _v1 = stack["product"]
        entry = service.get_entry(ctx, product_entry)
        with self.assertRaises(service.LibraryConflict):
            service.publish(ctx, product_entry, entry["draft"]["revision"])

    def test_blocked_validation_produces_no_partial_version(self) -> None:
        ctx = self.ctx()
        entry = service.create_entry(
            ctx,
            entry_type=ENTRY_PRODUCT,
            display_name="缺字段产品",
            content={"name": "X", "description": "", "audience": "", "must_match": ["Logo 完整"]},
        )
        with self.assertRaises(service.LibraryValidationError) as caught:
            service.publish(ctx, entry["id"], entry["draft"]["revision"])
        codes = {item["code"] for item in caught.exception.report["blocking"]}
        self.assertIn("must_match_not_structured", codes)
        after = service.get_entry(ctx, entry["id"])
        self.assertEqual(after["versions"], [])
        self.assertIsNone(after["current_version_id"])
        self.assertEqual(after["draft"]["state"], DRAFT_VALIDATION_FAILED)

    def test_validate_projects_draft_state_without_publishing(self) -> None:
        ctx = self.ctx()
        entry = service.create_entry(
            ctx, entry_type=ENTRY_PERSON, display_name="Nova", content={"name": "Nova"}
        )
        failed = service.validate_draft(ctx, entry["id"], expected_revision=entry["draft"]["revision"])
        self.assertFalse(failed["ok"])
        self.assertEqual(service.get_entry(ctx, entry["id"])["draft"]["state"], DRAFT_VALIDATION_FAILED)

        ref_ctx = ctx
        _ref_entry, ref_version = self._reference_version(ref_ctx, name="NovaRef")
        draft = service.get_entry(ctx, entry["id"])["draft"]
        saved = service.save_draft(ctx, entry["id"], self._person_content(ref_version["id"]), draft["revision"])
        passed = service.validate_draft(ctx, entry["id"], expected_revision=saved["revision"])
        self.assertTrue(passed["ok"], passed["blocking"])
        self.assertEqual(service.get_entry(ctx, entry["id"])["draft"]["state"], DRAFT_READY)
        self.assertEqual(service.get_entry(ctx, entry["id"])["versions"], [])

    # ------------------------------------------------------------------
    # 2. 身份边界：Offer / Landing Page / 映射不得藏进 ProductVersion
    # ------------------------------------------------------------------

    def test_product_version_refuses_offer_price_variant_and_store_mapping_fields(self) -> None:
        report = validate_content(
            ENTRY_PRODUCT,
            {
                "name": "X",
                "description": "d",
                "audience": "a",
                "price": "19.90",
                "currency": "USD",
                "default_variant": "first",
                "variant_id": "v_1",
                "landing_page_url": "https://example.com",
                "store_mapping": {"shop": "demo"},
                "shopify_token": "shpat_secret",
            },
        )
        paths = {finding.path for finding in report.blocking}
        self.assertEqual(
            paths,
            {
                "price",
                "currency",
                "default_variant",
                "variant_id",
                "landing_page_url",
                "store_mapping",
                "shopify_token",
            },
        )

    def test_offer_version_refuses_inline_external_object_and_price(self) -> None:
        report = validate_content(
            ENTRY_OFFER,
            {
                "product_version_id": "libver_x",
                "item": {"sku": "S", "quantity": 1},
                "landing_page_url": "https://example.com",
                "market_scope": ["US"],
                "valid_from": "2026-01-01",
                "external_product_id": "gid://shopify/Product/1",
                "external_variant_id": "gid://shopify/ProductVariant/1",
                "data_source_version_id": "dsv_1",
                "price": "19.90",
            },
        )
        codes = {finding.code for finding in report.blocking}
        self.assertEqual(codes, {"forbidden_field"})
        self.assertEqual(
            {finding.path for finding in report.blocking},
            {"external_product_id", "external_variant_id", "data_source_version_id", "price"},
        )

    def test_market_mapping_requires_explicit_external_object_and_data_source(self) -> None:
        report = validate_content(
            ENTRY_MARKET_MAPPING,
            {"offer_version_id": "libver_x", "market": "US", "execution_mode": "mock"},
        )
        codes = {finding.code for finding in report.blocking}
        self.assertIn("mapping_data_source", codes)
        self.assertIn("mapping_external_object", codes)

    def test_selection_rule_refuses_label_and_filename_matching(self) -> None:
        report = validate_content(
            ENTRY_SELECTION_RULE,
            {
                "rules": [
                    {
                        "match": {"label": "白底", "filename": "a.png"},
                        "group_version_id": "libver_g",
                        "priority": 0,
                    }
                ]
            },
        )
        codes = {finding.code for finding in report.blocking}
        self.assertIn("rule_forbidden_match", codes)
        self.assertIn("rule_match_stable_id", codes)

    def test_must_match_requires_structured_and_user_confirmed_requirements(self) -> None:
        unconfirmed = validate_content(
            ENTRY_PRODUCT,
            {
                "name": "X",
                "description": "d",
                "audience": "a",
                "must_match": [
                    {
                        "id": "mm1",
                        "type": "visual",
                        "description": "模型观察到的要求",
                        "severity": "blocking",
                        "viewpoints": ["front"],
                        "qc_method": "human_review",
                        "proposed_by": "ai",
                        "confirmed": False,
                    }
                ],
            },
        )
        self.assertIn("must_match_unconfirmed", {f.code for f in unconfirmed.blocking})

    # ------------------------------------------------------------------
    # 3. 角色边界（契约 §4）
    # ------------------------------------------------------------------

    def test_offer_publication_requires_owner_and_member_is_refused(self) -> None:
        ctx = self.ctx()
        owner = self.owner_ctx(ctx)
        _ref_entry, ref_version = self._reference_version(ctx)
        _group_entry, group_version = self._group_version(ctx, ref_version["id"])
        _product_entry, product_version = self._publish(
            ctx, ENTRY_PRODUCT, "Alpha", self._product_content(ref_version["id"], group_version["id"])
        )
        entry = service.create_entry(
            ctx,
            entry_type=ENTRY_OFFER,
            display_name="Alpha 单品",
            content=self._offer_content(product_version["id"]),
        )
        with self.assertRaises(service.LibraryForbidden):
            service.publish(ctx, entry["id"], entry["draft"]["revision"])
        self.assertEqual(service.get_entry(ctx, entry["id"])["versions"], [])

        published = service.publish(owner, entry["id"], entry["draft"]["revision"])
        self.assertEqual(published["version_no"], 1)

    def test_withdraw_requires_owner(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        _ref_entry, ref_version = stack["reference"]
        with self.assertRaises(service.LibraryForbidden):
            service.withdraw_version(ctx, ref_version["id"], reason="member 不应有权撤销")
        self.assertEqual(service.get_version(ctx, ref_version["id"])["state"], VERSION_EFFECTIVE)

    # ------------------------------------------------------------------
    # 4. 租户隔离负向测试
    # ------------------------------------------------------------------

    def test_cross_workspace_read_bind_and_reference_all_fail_closed(self) -> None:
        stack = self._full_stack()
        _ref_entry_a, ref_version_a = stack["reference"]
        product_entry_a, _product_version_a = stack["product"]

        ctx_b = self.ctx(role=ROLE_OWNER, actor="user_b")
        # 读取另一个 Workspace 的版本/条目：按不存在处理，不泄露存在性。
        with self.assertRaises(service.LibraryNotFound):
            service.get_version(ctx_b, ref_version_a["id"])
        with self.assertRaises(service.LibraryNotFound):
            service.get_entry(ctx_b, product_entry_a)

        # 跨 Workspace Bind 不提供。
        _ref_entry_b, ref_version_b = self._reference_version(ctx_b, name="RefB")
        _group_entry_b, group_version_b = self._group_version(ctx_b, ref_version_b["id"])
        entry_b = service.create_entry(
            ctx_b, entry_type=ENTRY_PRODUCT, display_name="B 产品", content={}
        )
        with self.assertRaises(service.LibraryNotFound):
            service.bind_reference(
                ctx_b,
                entry_b["id"],
                entry_b["draft"]["revision"],
                reference_version_id=ref_version_a["id"],
                purpose="hero",
                viewpoint="front",
            )

        # 直接在内容里引用另一个 Workspace 的版本：阻断，且措辞为"不存在"。
        content = self._product_content(ref_version_a["id"], group_version_b["id"])
        draft_b = service.save_draft(ctx_b, entry_b["id"], content, entry_b["draft"]["revision"])
        report = service.validate_draft(ctx_b, entry_b["id"], expected_revision=draft_b["revision"])
        codes = {item["code"] for item in report["blocking"]}
        self.assertIn("dependency_not_found", codes)

    def test_identical_file_in_two_workspaces_creates_independent_objects(self) -> None:
        raw = png_bytes(colour=(9, 9, 9))
        ctx_a = self.ctx()
        ctx_b = self.ctx(actor="user_b")
        facts_a = service.upload_reference_object(ctx_a, raw=raw, declared_mime="image/png", filename="same.png")
        facts_b = service.upload_reference_object(ctx_b, raw=raw, declared_mime="image/png", filename="same.png")

        self.assertEqual(facts_a["content_sha256"], facts_b["content_sha256"])
        self.assertNotEqual(facts_a["object_id"], facts_b["object_id"])
        self.assertFalse(facts_a["reused_object"])
        self.assertFalse(facts_b["reused_object"])
        with platform_session() as session:
            rows = session.scalars(
                select(ReferenceObjectRecord).where(
                    ReferenceObjectRecord.content_sha256 == facts_a["content_sha256"]
                )
            ).all()
            workspaces = {row.workspace_id for row in rows}
        self.assertIn(ctx_a.workspace_id, workspaces)
        self.assertIn(ctx_b.workspace_id, workspaces)

    def test_same_file_reused_in_one_workspace_keeps_bindings_independent(self) -> None:
        ctx = self.ctx()
        raw = png_bytes(colour=(3, 3, 3))
        first = service.upload_reference_object(ctx, raw=raw, declared_mime="image/png", filename="a.png")
        second = service.upload_reference_object(ctx, raw=raw, declared_mime="image/png", filename="b.png")
        self.assertEqual(first["object_id"], second["object_id"])
        self.assertTrue(second["reused_object"])

        _ref_entry, ref_version = self._reference_version(ctx, name="Shared", raw=raw)
        _group_entry, group_version = self._group_version(ctx, ref_version["id"])
        _product_entry, product_version = self._publish(
            ctx, ENTRY_PRODUCT, "Alpha", self._product_content(ref_version["id"], group_version["id"])
        )
        _person_entry, person_version = self._publish(
            ctx, ENTRY_PERSON, "Nova", self._person_content(ref_version["id"])
        )

        product_bindings = service.list_bindings(ctx, product_version["id"])
        person_bindings = service.list_bindings(ctx, person_version["id"])
        self.assertEqual([b["purpose"] for b in product_bindings], ["hero"])
        self.assertEqual([b["purpose"] for b in person_bindings], ["face"])
        # 同一个文件 hash，不同 Binding 保持独立行。
        self.assertNotEqual(product_bindings[0]["id"], person_bindings[0]["id"])
        self.assertEqual(
            product_bindings[0]["reference_version_id"], person_bindings[0]["reference_version_id"]
        )

    # ------------------------------------------------------------------
    # 5. quarantine → 受控派生（安全 fixture）
    # ------------------------------------------------------------------

    def test_upload_gate_releases_controlled_derivative_and_strips_exif(self) -> None:
        ctx = self.ctx()
        raw = jpeg_with_exif()
        facts = service.upload_reference_object(
            ctx, raw=raw, declared_mime="image/jpeg", filename="exif.jpg"
        )
        names = {check["name"] for check in facts["checks"]}
        self.assertEqual(
            names,
            {
                "byte_size",
                "object_integrity",
                "malware_scan",
                "decode",
                "declared_mime_matches_decoded",
                "pixel_limits",
                "exif_privacy",
                "content_safety",
            },
        )
        self.assertTrue(all(check["passed"] for check in facts["checks"]))
        self.assertEqual(facts["mime_type"], "image/jpeg")
        self.assertEqual((facts["width"], facts["height"]), (120, 90))

        with platform_session() as session:
            record = session.get(ReferenceObjectRecord, facts["object_id"])
            self.assertEqual(record.state, references.STATE_RELEASED)
            self.assertTrue(record.had_exif)
            self.assertTrue(record.derivative_object_key)
            self.assertNotEqual(record.derivative_sha256, record.content_sha256)
            self.assertFalse(references.derivative_has_exif(record))
            original_key = record.original_object_key
            derivative_key = record.derivative_object_key
        # 原件仍在私有对象边界内，且与受控派生是两个不同文件。
        self.assertTrue(Path(original_key).exists())
        self.assertNotEqual(original_key, derivative_key)
        with Image.open(derivative_key) as derived:
            self.assertEqual(derived.format, "JPEG")
            self.assertFalse(dict(derived.getexif()))

    def test_provider_only_ever_receives_the_controlled_derivative(self) -> None:
        ctx = self.ctx()
        raw = jpeg_with_exif()
        _entry_id, version = self._reference_version(ctx, name="Provider", raw=raw)
        payload = service.provider_reference_payload(ctx, version["id"])
        with platform_session() as session:
            record = session.scalars(
                select(ReferenceObjectRecord).where(
                    ReferenceObjectRecord.workspace_id == ctx.workspace_id,
                    ReferenceObjectRecord.content_sha256 == references.sha256_bytes(raw),
                )
            ).one()
            original_key = record.original_object_key
            derivative_key = record.derivative_object_key
        self.assertEqual(payload["object_key"], derivative_key)
        self.assertNotEqual(payload["object_key"], original_key)
        self.assertEqual(payload["mime_type"], "image/jpeg")
        self.assertEqual(payload["execution_mode"], "mock")

    def test_upload_gate_rejects_malware_mime_mismatch_malformed_and_flagged_content(self) -> None:
        cases: dict[str, dict] = {
            "malware_scan": dict(raw=references.EICAR_SIGNATURE + b"\x00" * 64, declared_mime="image/png"),
            "declared_mime_matches_decoded": dict(raw=png_bytes(), declared_mime="image/jpeg"),
            "decode": dict(raw=b"\x89PNG\r\n\x1a\n" + b"not-a-real-png" * 4, declared_mime="image/png"),
            "content_safety": dict(raw=png_bytes(colour=(1, 2, 3)), declared_mime="image/png", safety_scenario="flagged"),
        }
        for expected_check, kwargs in cases.items():
            with self.subTest(check=expected_check):
                ctx = self.ctx()
                with self.assertRaises(references.ReferenceGateError) as caught:
                    service.upload_reference_object(ctx, filename="bad", **kwargs)
                self.assertIn(expected_check, str(caught.exception))
                with platform_session() as session:
                    row = session.scalars(
                        select(ReferenceObjectRecord).where(
                            ReferenceObjectRecord.workspace_id == ctx.workspace_id
                        )
                    ).one()
                    # 拒绝的证据被提交，不产生没有记录的孤儿原件。
                    self.assertEqual(row.state, references.STATE_REJECTED)
                    self.assertFalse(row.derivative_object_key)
                    audit = session.scalars(
                        select(LibraryAuditRecord).where(
                            LibraryAuditRecord.workspace_id == ctx.workspace_id,
                            LibraryAuditRecord.command == "upload_reference",
                        )
                    ).one()
                    self.assertEqual(audit.result, "blocked")

    def test_upload_gate_refuses_oversized_and_unknown_mime_before_storing(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(references.ReferenceGateError):
            service.upload_reference_object(
                ctx, raw=b"x" * (config.LIBRARY_MAX_REFERENCE_BYTES + 1), declared_mime="image/png"
            )
        with self.assertRaises(references.ReferenceGateError):
            service.upload_reference_object(ctx, raw=png_bytes(), declared_mime="application/pdf")

    def test_real_content_safety_adapter_never_falls_back_to_mock(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(references.ReferenceGateError) as caught:
            service.upload_reference_object(
                ctx, raw=png_bytes(), declared_mime="image/png", execution_mode="real"
            )
        self.assertIn("回落", str(caught.exception))
        with platform_session() as session:
            row = session.scalars(
                select(ReferenceObjectRecord).where(ReferenceObjectRecord.workspace_id == ctx.workspace_id)
            ).one()
            # 检查无法执行时对象停在 quarantine，而不是被当成"已拒绝"或"已放行"。
            self.assertEqual(row.state, references.STATE_QUARANTINED)

    def test_reference_version_requires_released_object_and_granted_unexpired_rights(self) -> None:
        ctx = self.ctx()
        facts = service.upload_reference_object(ctx, raw=png_bytes(), declared_mime="image/png")
        base = {
            "object_id": facts["object_id"],
            "source": "upload",
            "rights": {"state": "granted", "scope": "s", "declared_by": ctx.initiator_id, "valid_to": FUTURE},
        }
        cases = [
            ("missing_object", "reference_object_not_found", {**base, "object_id": "refobj_missing"}),
            ("revoked_rights", "reference_rights_ineligible", {**base, "rights": {**base["rights"], "state": "revoked"}}),
            ("expired_rights", "reference_rights_ineligible", {**base, "rights": {**base["rights"], "valid_to": PAST}}),
        ]
        for label, expected_code, content in cases:
            with self.subTest(case=label):
                entry = service.create_entry(
                    ctx, entry_type=ENTRY_REFERENCE_ASSET, display_name="Bad ref", content=content
                )
                with self.assertRaises(service.LibraryValidationError) as caught:
                    service.publish(ctx, entry["id"], entry["draft"]["revision"])
                self.assertIn(expected_code, {i["code"] for i in caught.exception.report["blocking"]})

    def test_expired_reference_rights_block_downstream_product_publication(self) -> None:
        ctx = self.ctx()
        facts = service.upload_reference_object(ctx, raw=png_bytes(), declared_mime="image/png")
        # 发布时仍在有效期内。
        ref_entry, ref_version = self._publish(
            ctx,
            ENTRY_REFERENCE_ASSET,
            "即将过期的参考",
            {
                "object_id": facts["object_id"],
                "source": "upload",
                "file": {
                    "content_sha256": facts["content_sha256"],
                    "mime_type": facts["mime_type"],
                    "byte_size": facts["byte_size"],
                    "width": facts["width"],
                    "height": facts["height"],
                },
                "rights": {
                    "state": "granted",
                    "scope": "s",
                    "declared_by": ctx.initiator_id,
                    "valid_to": (date.today() + timedelta(days=1)).isoformat(),
                },
                "gate_checks": facts.get("checks", []),
                "derivative": facts.get("derivative", {}),
            },
        )
        _group_entry, group_version = self._group_version(ctx, ref_version["id"])
        entry = service.create_entry(
            ctx,
            entry_type=ENTRY_PRODUCT,
            display_name="Alpha",
            content=self._product_content(ref_version["id"], group_version["id"]),
        )
        # 以"两天后"的时钟重新校验：上游权利已过期，新的下游发布必须失败关闭。
        with self.assertRaises(service.LibraryValidationError) as caught:
            service.publish(
                ctx, entry["id"], entry["draft"]["revision"], today=date.today() + timedelta(days=2)
            )
        self.assertIn(
            "reference_rights_ineligible", {i["code"] for i in caught.exception.report["blocking"]}
        )
        self.assertEqual(service.get_entry(ctx, entry["id"])["versions"], [])

    def test_person_licensed_talent_requires_evidence_validity_and_unexpired_rights(self) -> None:
        ctx = self.ctx()
        _ref_entry, ref_version = self._reference_version(ctx)
        missing_evidence = self._person_content(
            ref_version["id"],
            person_type="licensed_talent",
            rights={"state": "granted", "scope": "brand", "regions": ["US"], "channels": ["meta"]},
        )
        report = validate_content(ENTRY_PERSON, missing_evidence)
        codes = {f.code for f in report.blocking}
        self.assertIn("person_rights_evidence", codes)
        self.assertIn("person_rights_valid_to", codes)

        expired = self._person_content(
            ref_version["id"],
            person_type="licensed_talent",
            rights={
                "state": "granted",
                "scope": "brand",
                "regions": ["US"],
                "channels": ["meta"],
                "evidence": "contract://2026-01",
                "valid_to": PAST,
            },
        )
        self.assertIn("person_rights_expired", {f.code for f in validate_content(ENTRY_PERSON, expired).blocking})

    # ------------------------------------------------------------------
    # 6. 循环引用与归档
    # ------------------------------------------------------------------

    def test_self_referencing_version_fails_closed_as_circular(self) -> None:
        ctx = self.ctx()
        _ref_entry, ref_version = self._reference_version(ctx)
        group_entry, group_version = self._group_version(ctx, ref_version["id"])
        draft = service.get_entry(ctx, group_entry)["draft"]
        saved = service.save_draft(
            ctx,
            group_entry,
            {"members": [{"reference_version_id": group_version["id"], "role": "white_studio"}]},
            draft["revision"],
        )
        report = service.validate_draft(ctx, group_entry, expected_revision=saved["revision"])
        self.assertIn("circular_reference", {i["code"] for i in report["blocking"]})

    def test_archive_requires_owner(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        ref_entry, _ref_version = stack["reference"]
        ref_rev = service.get_entry(ctx, ref_entry)["draft"]["revision"]
        with self.assertRaises(service.LibraryForbidden):
            service.archive_entry(ctx, ref_entry, expected_revision=ref_rev)
        self.assertEqual(service.get_entry(ctx, ref_entry)["lifecycle"], "active")
        with platform_session() as session:
            refused = session.scalars(
                select(LibraryAuditRecord).where(
                    LibraryAuditRecord.workspace_id == ctx.workspace_id,
                    LibraryAuditRecord.command == "archive",
                )
            ).one()
            # 被拒绝的尝试同样留下审计，不会因为回滚而消失。
            self.assertEqual(refused.result, "forbidden")

    def test_archived_entry_blocks_new_references_and_further_editing(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        owner = stack["owner"]
        ref_entry, ref_version = stack["reference"]
        ref_rev = service.get_entry(ctx, ref_entry)["draft"]["revision"]
        archived = service.archive_entry(owner, ref_entry, expected_revision=ref_rev)
        self.assertEqual(archived["lifecycle"], LIFECYCLE_ARCHIVED)
        # 归档不删除版本。
        self.assertEqual(len(archived["versions"]), 1)
        self.assertEqual(service.get_version(ctx, ref_version["id"])["state"], VERSION_EFFECTIVE)

        with self.assertRaises(service.LibraryConflict):
            service.save_draft(ctx, ref_entry, {"x": 1}, archived["draft"]["revision"])

        new_product = service.create_entry(
            ctx,
            entry_type=ENTRY_PRODUCT,
            display_name="引用已归档参考",
            content=self._product_content(ref_version["id"], stack["group"][1]["id"]),
        )
        report = service.validate_draft(ctx, new_product["id"], expected_revision=new_product["draft"]["revision"])
        self.assertIn("dependency_archived", {i["code"] for i in report["blocking"]})

    # ------------------------------------------------------------------
    # 7. 并发 expected-revision（契约 §3.3）
    # ------------------------------------------------------------------

    def test_concurrent_draft_writes_conflict_with_a_field_level_diff(self) -> None:
        ctx = self.ctx()
        entry = service.create_entry(
            ctx, entry_type=ENTRY_PRODUCT, display_name="并发产品", content={"name": "base", "description": "v0"}
        )
        revision = entry["draft"]["revision"]
        service.save_draft(ctx, entry["id"], {"name": "base", "description": "用户 A 的改动"}, revision)

        with self.assertRaises(service.LibraryConflict) as caught:
            service.save_draft(ctx, entry["id"], {"name": "base", "description": "用户 B 的改动"}, revision)
        detail = caught.exception.detail
        self.assertEqual(detail["expected_revision"], revision)
        self.assertEqual(detail["current_revision"], revision + 1)
        self.assertEqual(
            detail["field_diff"]["description"],
            {"base": "用户 A 的改动", "attempted": "用户 B 的改动"},
        )
        # 最后写入者没有静默覆盖。
        self.assertEqual(
            service.get_entry(ctx, entry["id"])["draft"]["content"]["description"], "用户 A 的改动"
        )

    def test_publish_with_stale_revision_is_refused(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        product_entry, _v1 = stack["product"]
        entry = service.get_entry(ctx, product_entry)
        stale = entry["draft"]["revision"]
        edited = dict(entry["draft"]["content"])
        edited["description"] = "新描述"
        service.save_draft(ctx, product_entry, edited, stale)
        with self.assertRaises(service.LibraryConflict):
            service.publish(ctx, product_entry, stale)
        self.assertEqual(len(service.get_entry(ctx, product_entry)["versions"]), 1)

    def test_archive_advances_revision_so_concurrent_save_fails(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        owner = stack["owner"]
        ref_entry, _ref_version = stack["reference"]
        pre_archive_rev = service.get_entry(ctx, ref_entry)["draft"]["revision"]
        service.archive_entry(owner, ref_entry, expected_revision=pre_archive_rev)
        with self.assertRaises(service.LibraryConflict):
            service.save_draft(ctx, ref_entry, {"x": 1}, pre_archive_rev)

    def test_superseded_version_can_be_withdrawn(self) -> None:
        ctx = self.ctx()
        owner = self.owner_ctx(ctx)
        _ref_entry, ref_version = self._reference_version(ctx)
        _group_entry, group_version = self._group_version(ctx, ref_version["id"])
        product_entry, v1 = self._publish(
            ctx, ENTRY_PRODUCT, "撤销已替代版本",
            self._product_content(ref_version["id"], group_version["id"]),
        )
        entry = service.get_entry(ctx, product_entry)
        service.save_draft(ctx, product_entry, {**entry["draft"]["content"], "description": "v2"}, entry["draft"]["revision"])
        entry = service.get_entry(ctx, product_entry)
        service.validate_draft(ctx, product_entry, expected_revision=entry["draft"]["revision"])
        v2 = service.publish(ctx, product_entry, entry["draft"]["revision"])
        refreshed_v1 = service.get_version(ctx, v1["id"])
        self.assertEqual(refreshed_v1["state"], "superseded")
        result = service.withdraw_version(owner, v1["id"], reason="安全问题须撤回")
        self.assertEqual(result["state"], "withdrawn")

    # ------------------------------------------------------------------
    # 8. Withdraw 传播（契约 §14）
    # ------------------------------------------------------------------

    def test_withdraw_propagates_downstream_preserves_history_and_lists_impact(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        owner = stack["owner"]
        _ref_entry, ref_version = stack["reference"]
        _group_entry, group_version = stack["group"]
        _rule_entry, rule_version = stack["rule"]
        product_entry, product_version = stack["product"]
        _offer_entry, offer_version = stack["offer"]
        # mapping_version is None: the stack's mapping stays a draft
        _person_entry, person_version = stack["person"]

        # 反向闭包：ref ← group ← {rule, product}；rule ← product；product ← offer；ref ← person。
        # mapping has no published version so is not in the dependency chain.
        before = service.impact_analysis(ctx, ref_version["id"])
        impacted_before = {item["version_id"] for item in before["dependents"]}
        self.assertEqual(
            impacted_before,
            {
                group_version["id"],
                rule_version["id"],
                product_version["id"],
                person_version["id"],
                offer_version["id"],
            },
        )

        result = service.withdraw_version(owner, ref_version["id"], reason="肖像授权被撤回")
        self.assertEqual(result["state"], VERSION_WITHDRAWN)
        self.assertEqual(result["eligibility"], ELIGIBILITY_BLOCKED)
        self.assertEqual({item["version_id"] for item in result["dependents"]}, impacted_before)

        # 下游版本被投影为 blocked，但内容与状态历史都保留。
        for version_id in impacted_before:
            downstream = service.get_version(ctx, version_id)
            self.assertEqual(downstream["eligibility"], ELIGIBILITY_BLOCKED)
            self.assertNotEqual(downstream["state"], VERSION_WITHDRAWN)
        self.assertEqual(
            service.get_version(ctx, product_version["id"])["content"], product_version["content"]
        )
        # 上游条目的当前版本投影被清空，但历史版本仍在。
        ref_entry_after = service.get_entry(ctx, _ref_entry)
        self.assertIsNone(ref_entry_after["current_version_id"])
        self.assertEqual(len(ref_entry_after["versions"]), 1)

        # 新的外部动作被阻止。
        with self.assertRaises(service.LibraryConflict):
            service.provider_reference_payload(ctx, ref_version["id"])

        # 新的下游发布引用被撤销的版本时失败关闭。
        fresh = service.create_entry(
            ctx,
            entry_type=ENTRY_PRODUCT,
            display_name="新产品",
            content=self._product_content(ref_version["id"], group_version["id"]),
        )
        with self.assertRaises(service.LibraryValidationError) as caught:
            service.publish(ctx, fresh["id"], fresh["draft"]["revision"])
        codes = {i["code"] for i in caught.exception.report["blocking"]}
        self.assertIn("dependency_withdrawn", codes)

    def test_withdraw_emits_upstream_events_and_an_auditable_trail(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        owner = stack["owner"]
        _ref_entry, ref_version = stack["reference"]
        product_entry, product_version = stack["product"]

        service.withdraw_version(owner, ref_version["id"], reason="事实错误")

        product_events = service.list_events(ctx, product_entry)
        types = [event["event_type"] for event in product_events]
        self.assertIn("upstream_withdrawn", types)
        upstream = [e for e in product_events if e["event_type"] == "upstream_withdrawn"][0]
        self.assertEqual(upstream["payload"]["withdrawn_version_id"], ref_version["id"])
        self.assertEqual(upstream["payload"]["reason"], "事实错误")
        # 事件是追加式的，序号严格递增。
        self.assertEqual([e["sequence"] for e in product_events], sorted(e["sequence"] for e in product_events))
        self.assertEqual(len(set(e["sequence"] for e in product_events)), len(product_events))

        with platform_session() as session:
            audit = session.scalars(
                select(LibraryAuditRecord).where(
                    LibraryAuditRecord.workspace_id == ctx.workspace_id,
                    LibraryAuditRecord.command == "withdraw",
                )
            ).one()
            self.assertEqual(audit.result, "ok")
            self.assertEqual(audit.initiator_id, owner.initiator_id)
            self.assertEqual(audit.initiator_role, ROLE_OWNER)
            self.assertIn(product_version["id"], audit.detail_json["impacted"])
            self.assertEqual(audit.detail_json["reason"], "事实错误")

    def test_repeated_withdraw_is_idempotent(self) -> None:
        stack = self._full_stack()
        owner = stack["owner"]
        ctx = stack["ctx"]
        _ref_entry, ref_version = stack["reference"]
        first = service.withdraw_version(owner, ref_version["id"], reason="法务要求")
        second = service.withdraw_version(owner, ref_version["id"], reason="法务要求")
        self.assertEqual(first["dependents"], second["dependents"])
        events = service.list_events(ctx, _ref_entry)
        self.assertEqual(len([e for e in events if e["event_type"] == "withdrawn"]), 1)

    # ------------------------------------------------------------------
    # 9. Bind / Fork / Clone 谱系（契约 §12）
    # ------------------------------------------------------------------

    def test_bind_adds_an_explicit_binding_to_the_draft_under_optimistic_lock(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        _ref_entry, ref_version = self._reference_version(ctx)
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Alpha", content={})
        draft = service.bind_reference(
            ctx,
            entry["id"],
            entry["draft"]["revision"],
            reference_version_id=ref_version["id"],
            purpose="hero",
            viewpoint="front",
            priority=1,
        )
        self.assertEqual(draft["revision"], entry["draft"]["revision"] + 1)
        self.assertEqual(draft["content"]["reference_bindings"][0]["reference_version_id"], ref_version["id"])

        # 同一 purpose/viewpoint 重复绑定被拒绝。
        with self.assertRaises(service.LibraryConflict):
            service.bind_reference(
                ctx,
                entry["id"],
                draft["revision"],
                reference_version_id=ref_version["id"],
                purpose="hero",
                viewpoint="front",
            )
        # 过期 revision 也被拒绝。
        with self.assertRaises(service.LibraryConflict):
            service.bind_reference(
                ctx,
                entry["id"],
                entry["draft"]["revision"],
                reference_version_id=ref_version["id"],
                purpose="detail",
                viewpoint="back",
            )

    def test_fork_creates_an_independent_entry_with_recorded_lineage(self) -> None:
        stack = self._full_stack()
        ctx = stack["owner"]
        person_entry, person_version = stack["person"]

        forked = service.fork_version(ctx, person_version["id"], display_name="Nova Fork")
        self.assertNotEqual(forked["id"], person_entry)
        self.assertEqual(forked["draft"]["origin_command"], "fork")
        self.assertEqual(forked["draft"]["source_version_id"], person_version["id"])
        self.assertEqual(forked["versions"], [])

        published = service.publish(ctx, forked["id"], forked["draft"]["revision"])
        self.assertEqual(published["version_no"], 1)
        self.assertEqual(published["origin_command"], "fork")
        self.assertEqual(published["source_version_id"], person_version["id"])
        # 源条目完全不受影响。
        source = service.get_entry(ctx, person_entry)
        self.assertEqual(len(source["versions"]), 1)
        self.assertEqual(source["current_version_id"], person_version["id"])

    def test_clone_product_copies_only_low_risk_content_by_default(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        _ref_entry, ref_version = self._reference_version(ctx)
        _group_entry, group_version = self._group_version(ctx, ref_version["id"])
        content = self._product_content(ref_version["id"], group_version["id"], brand_sku="ALPHA-SKU")
        product_entry, product_version = self._publish(ctx, ENTRY_PRODUCT, "Alpha", content)

        clone = service.clone_product(ctx, product_version["id"], display_name="Alpha 副本")
        cloned = clone["draft"]["content"]
        self.assertEqual(clone["draft"]["origin_command"], "clone")
        self.assertEqual(clone["draft"]["source_version_id"], product_version["id"])
        # 低风险内容被复制。
        self.assertEqual(cloned["description"], content["description"])
        self.assertEqual(cloned["must_match"], content["must_match"])
        self.assertEqual(cloned["reference_bindings"], content["reference_bindings"])
        self.assertEqual(cloned["name"], "Alpha 副本")
        # brand_sku 是可选项，默认不复制。
        self.assertNotIn("brand_sku", cloned)
        self.assertIn("brand_sku", clone["clone"]["skipped_fields"])

        with_optional = service.clone_product(
            ctx, product_version["id"], display_name="Alpha 副本 2", include_optional=["brand_sku"]
        )
        self.assertEqual(with_optional["draft"]["content"]["brand_sku"], "ALPHA-SKU")

    def test_clone_product_never_copies_offer_price_mapping_or_landing_page(self) -> None:
        ctx = self.ctx()
        owner = self.owner_ctx(ctx)
        _ref_entry, ref_version = self._reference_version(ctx)
        _group_entry, group_version = self._group_version(ctx, ref_version["id"])
        product_entry, product_version = self._publish(
            ctx, ENTRY_PRODUCT, "Alpha", self._product_content(ref_version["id"], group_version["id"])
        )
        offer_entry, offer_version = self._publish(
            owner, ENTRY_OFFER, "Alpha 单品", self._offer_content(product_version["id"])
        )

        for field in ("offers", "market_source_mappings", "landing_page_url", "price"):
            with self.subTest(field=field):
                with self.assertRaises(service.LibraryError):
                    service.clone_product(
                        owner, product_version["id"], display_name="不合法副本", include_optional=[field]
                    )

        clone = service.clone_product(owner, product_version["id"], display_name="Alpha 副本")
        self.assertNotIn("landing_page_url", clone["draft"]["content"])
        # Offer 仍然只挂在源 ProductVersion 上，副本没有任何 Offer。
        self.assertEqual(service.get_version(ctx, offer_version["id"])["content"]["product_version_id"], product_version["id"])
        offers = [e for e in service.list_entries(ctx, entry_type=ENTRY_OFFER)]
        self.assertEqual([e["id"] for e in offers], [offer_entry])

    # ------------------------------------------------------------------
    # 10. 审计与事件
    # ------------------------------------------------------------------

    def test_commands_write_a_single_actor_audit_trail_without_secrets(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        owner = stack["owner"]
        product_entry, _product_version = stack["product"]
        product_rev = service.get_entry(ctx, product_entry)["draft"]["revision"]
        service.archive_entry(owner, product_entry, expected_revision=product_rev)

        with platform_session() as session:
            rows = session.scalars(
                select(LibraryAuditRecord).where(LibraryAuditRecord.workspace_id == ctx.workspace_id)
            ).all()
            commands = {row.command for row in rows}
            self.assertTrue(
                {"create_entry", "validate_draft", "publish", "upload_reference", "archive"} <= commands
            )
            for row in rows:
                self.assertEqual(row.trace_id, "trace-test")
                self.assertIn(row.initiator_role, {"owner", "member"})
                self.assertNotIn("shpat_", str(row.detail_json))
                # Dual-actor audit: executor defaults to "user" for request-side.
                self.assertEqual(row.executor, "user")
                self.assertTrue(row.request_id, "request_id must be non-empty")

    def test_version_lifecycle_is_recorded_as_append_only_events(self) -> None:
        stack = self._full_stack()
        ctx = stack["ctx"]
        product_entry, v1 = stack["product"]
        entry = service.get_entry(ctx, product_entry)
        edited = dict(entry["draft"]["content"])
        edited["description"] = "第二版描述"
        draft = service.save_draft(ctx, product_entry, edited, entry["draft"]["revision"])
        service.publish(ctx, product_entry, draft["revision"])

        events = service.list_events(ctx, product_entry)
        types = [event["event_type"] for event in events]
        self.assertEqual(types[0], "draft_created")
        self.assertEqual(types.count("published"), 2)
        self.assertEqual(types.count("superseded"), 1)
        superseded = [e for e in events if e["event_type"] == "superseded"][0]
        self.assertEqual(superseded["version_id"], v1["id"])
        self.assertEqual(superseded["from_state"], VERSION_EFFECTIVE)
        self.assertEqual(superseded["to_state"], VERSION_SUPERSEDED)


_LIBRARY_ENTRY_TYPES = {
    ENTRY_PRODUCT,
    ENTRY_OFFER,
    ENTRY_PERSON,
    ENTRY_REFERENCE_ASSET,
    ENTRY_REFERENCE_GROUP,
    ENTRY_SELECTION_RULE,
    ENTRY_MARKET_MAPPING,
}


class LibraryTenantAndMountGuardTests(LibraryFixtures):
    """租户与暴露面负向测试。

    这些断言的是**应用层**作用域和"接口没有被挂载"，不是 RLS，也不是身份认证。
    """

    def test_domain_service_never_provisions_a_workspace_on_demand(self) -> None:
        ghost = self.ctx(workspace="ws_never_provisioned", provision=False)
        with self.assertRaises(service.LibraryNotFound):
            service.create_entry(
                ghost, entry_type=ENTRY_PRODUCT, display_name="幽灵产品", content={}
            )
        with self.assertRaises(service.LibraryNotFound):
            service.upload_reference_object(ghost, raw=png_bytes(), declared_mime="image/png")
        with platform_session() as session:
            self.assertIsNone(session.get(WorkspaceRecord, "ws_never_provisioned"))
            self.assertEqual(
                session.scalars(
                    select(ReferenceObjectRecord).where(
                        ReferenceObjectRecord.workspace_id == "ws_never_provisioned"
                    )
                ).all(),
                [],
            )

    def test_library_router_is_mounted(self) -> None:
        """Verify /api/v1/library routes exist in the app route table, and
        the legacy /api/library router (removed in #27 Task 9) is gone.

        Reads the fully-resolved paths from ``app.openapi()`` rather than
        walking ``app.routes`` directly: FastAPI's nested-router wrapper
        (``_IncludedRouter``) exposes each sub-route's *relative* path, not
        its full mounted path, so reflecting on the route tree without
        re-deriving the accumulated prefix is fragile across Starlette
        versions -- the OpenAPI document is what the app actually serves.
        """
        from app.main import app

        all_paths = sorted(app.openapi()["paths"].keys())
        mounted = [p for p in all_paths if p.startswith("/api/v1/library")]
        self.assertIn("/api/v1/library/entry-types", mounted)
        self.assertIn("/api/v1/library/entries", mounted)

        legacy = [p for p in all_paths if p.startswith("/api/library")]
        self.assertEqual(legacy, [])

    def test_library_router_rejects_unauthenticated(self) -> None:
        """Call a v1 endpoint without TenantContext, expect 401; the removed
        legacy /api/library path must 404 instead of resolving at all."""
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/library/entries")
        self.assertEqual(response.status_code, 401)

        legacy_response = client.get("/api/library/entries")
        self.assertEqual(legacy_response.status_code, 404)

    def test_library_router_module_does_not_let_callers_pick_the_safety_adapter(self) -> None:
        # api.py 保留为 #17 的形状草案，但上传端点不得把 execution mode / Mock
        # 场景交给调用方；否则客户端可以自行关闭内容安全检查。
        from app.library import api

        params = set(api.upload_reference_object.__annotations__)
        self.assertNotIn("execution_mode", params)
        self.assertNotIn("safety_scenario", params)

    # ------------------------------------------------------------------
    # Role gates (#16 TenantContext migration)
    # ------------------------------------------------------------------

    def test_member_can_clone_product(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        ref_entry, ref_version = self._reference_version(ctx)
        group_entry, group_version = self._group_version(ctx, ref_version["id"])
        product_content = self._product_content(ref_version["id"], group_version["id"])
        _product_entry, product_version = self._publish(ctx, ENTRY_PRODUCT, "Clone Test", product_content)
        member = TenantContext(
            workspace_id=ctx.workspace_id, role=ROLE_MEMBER, initiator_id="member_x",
            request_id="test-req", trace_id="trace-test",
        )
        result = service.clone_product(member, product_version["id"], display_name="Cloned")
        self.assertEqual(result["entry_type"], ENTRY_PRODUCT)
        self.assertEqual(result["display_name"], "Cloned")

    def test_member_can_fork_version(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        ref_entry, ref_version = self._reference_version(ctx)
        group_entry, group_version = self._group_version(ctx, ref_version["id"])
        product_content = self._product_content(ref_version["id"], group_version["id"])
        _product_entry, product_version = self._publish(ctx, ENTRY_PRODUCT, "Fork Test", product_content)
        member = TenantContext(
            workspace_id=ctx.workspace_id, role=ROLE_MEMBER, initiator_id="member_x",
            request_id="test-req", trace_id="trace-test",
        )
        result = service.fork_version(member, product_version["id"], display_name="Forked")
        self.assertEqual(result["entry_type"], ENTRY_PRODUCT)
        self.assertEqual(result["display_name"], "Forked")

    def test_member_can_bind_reference(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        ref_entry, ref_version = self._reference_version(ctx)
        group_entry, group_version = self._group_version(ctx, ref_version["id"])
        product_content = self._product_content(ref_version["id"], group_version["id"])
        product_entry_result = service.create_entry(
            ctx, entry_type=ENTRY_PRODUCT, display_name="Bind Test", content=product_content,
        )
        member = TenantContext(
            workspace_id=ctx.workspace_id, role=ROLE_MEMBER, initiator_id="member_x",
            request_id="test-req", trace_id="trace-test",
        )
        result = service.bind_reference(
            member, product_entry_result["id"], product_entry_result["draft"]["revision"],
            reference_version_id=ref_version["id"],
            purpose="side", viewpoint="left",
        )
        bindings = result["content"].get("reference_bindings", [])
        bound_ids = {b["reference_version_id"] for b in bindings}
        self.assertIn(ref_version["id"], bound_ids)

    # ------------------------------------------------------------------
    # Streaming upload size limit
    # ------------------------------------------------------------------

    def test_upload_rejects_oversized_file_without_full_buffering(self) -> None:
        import asyncio
        from unittest.mock import MagicMock

        oversized_size = config.LIBRARY_MAX_REFERENCE_BYTES + 1
        remaining = oversized_size

        async def mock_read(size=8192):
            nonlocal remaining
            if remaining <= 0:
                return b""
            chunk = min(size, remaining)
            remaining -= chunk
            return b"\x00" * chunk

        mock_file = MagicMock()
        mock_file.read = mock_read
        mock_file.content_type = "image/png"
        mock_file.filename = "big.png"

        from app.library import api as library_api

        async def run():
            with self.assertRaises(service.UploadTooLarge):
                await library_api.upload_reference_object(
                    file=mock_file, context=self.ctx(), idempotency_key="upload-413-test",
                )

        asyncio.run(run())

    # ------------------------------------------------------------------
    # market_source_mapping fail-closed
    # ------------------------------------------------------------------

    def test_market_source_mapping_roundtrip(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        ref_entry, ref_version = self._reference_version(ctx)
        group_entry, group_version = self._group_version(ctx, ref_version["id"])
        product_content = self._product_content(ref_version["id"], group_version["id"])
        _product_entry, product_version = self._publish(ctx, ENTRY_PRODUCT, "Mapping Test", product_content)
        _offer_entry, offer_version = self._publish(
            ctx, ENTRY_OFFER, "Offer Test", self._offer_content(product_version["id"]),
        )
        # The mapping must name a published DataSource version of this
        # Workspace whose mode matches its own (#61).
        _source_entry, source_version = self._publish(
            ctx, ENTRY_DATA_SOURCE, "Mock source", {"adapter_kind": "mock", "execution_mode": "mock"},
        )
        mapping_content = {
            "offer_version_id": offer_version["id"],
            "data_source_version_id": source_version["id"],
            "external_object": {"kind": "variant", "id": "gid://shopify/ProductVariant/42"},
            "market": "US",
            "execution_mode": "mock",
        }
        entry = service.create_entry(
            ctx, entry_type=ENTRY_MARKET_MAPPING, display_name="Mapping", content=mapping_content,
        )
        revision = entry["draft"]["revision"]
        report = service.validate_draft(ctx, entry["id"], expected_revision=revision)
        self.assertTrue(report["ok"], report)
        version = service.publish(ctx, entry["id"], revision)
        self.assertEqual(version["state"], "effective")
        self.assertEqual(version["content"]["data_source_version_id"], source_version["id"])
        self.assertEqual(version["content"]["market"], "US")

    def test_market_source_mapping_rejects_missing_fields(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        entry = service.create_entry(
            ctx, entry_type=ENTRY_MARKET_MAPPING, display_name="Bad Mapping", content={},
        )
        report = service.validate_draft(ctx, entry["id"], expected_revision=entry["draft"]["revision"])
        self.assertFalse(report["ok"])
        codes = {f["code"] for f in report["blocking"]}
        self.assertIn("mapping_offer", codes)
        self.assertIn("mapping_data_source", codes)
        self.assertIn("mapping_external_object", codes)
        self.assertIn("mapping_market", codes)
        self.assertIn("mapping_execution_mode", codes)

    # ------------------------------------------------------------------
    # Withdrawal structured event
    # ------------------------------------------------------------------

    def test_withdrawal_emits_structured_event(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        ref_entry, ref_version = self._reference_version(ctx)
        group_entry, group_version = self._group_version(ctx, ref_version["id"])
        product_content = self._product_content(ref_version["id"], group_version["id"])
        _product_entry, product_version = self._publish(ctx, ENTRY_PRODUCT, "Withdraw Event Test", product_content)

        service.withdraw_version(ctx, ref_version["id"], reason="test withdrawal")

        events = service.list_events(ctx, ref_entry)
        withdrawal_events = [e for e in events if e["event_type"] == "version.withdrawn"]
        self.assertEqual(len(withdrawal_events), 1)
        payload = withdrawal_events[0]["payload"]
        self.assertEqual(payload["reason"], "test withdrawal")
        self.assertIn("impact", payload)
        self.assertIn("blocked_versions", payload["impact"])
        self.assertIn(product_version["id"], payload["impact"]["blocked_versions"])
        self.assertEqual(
            payload["downstream_event_contract"],
            list(WITHDRAWAL_DOWNSTREAM_EVENT_TYPES.values()),
        )

        downstream_types = set(WITHDRAWAL_DOWNSTREAM_EVENT_TYPES.values())
        downstream_events = [e for e in events if e["event_type"] in downstream_types]
        self.assertEqual(len(downstream_events), len(downstream_types))
        emitted_types = {e["event_type"] for e in downstream_events}
        self.assertEqual(emitted_types, downstream_types)
        for de in downstream_events:
            self.assertEqual(de["payload"]["withdrawn_version_id"], ref_version["id"])
            self.assertEqual(de["payload"]["reason"], "test withdrawal")
            self.assertIn("affected_entities", de["payload"])


class LibraryIdempotencyTests(LibraryFixtures):
    """Idempotency atomicity, fingerprint binding, and cross-command rejection."""

    def test_idempotent_replay_returns_cached_response(self) -> None:
        ctx = self.ctx()
        key = "idem-replay-test-001"
        content = {"name": "Replay", "description": "d", "audience": "a"}
        fp = service.compute_fingerprint({"entry_type": ENTRY_PRODUCT, "display_name": "Replay", "content": content})
        first = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Replay", content=content,
                                     idempotency_key=key, request_fingerprint=fp)
        second = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Replay", content=content,
                                      idempotency_key=key, request_fingerprint=fp)
        self.assertEqual(first, second)

    def test_idempotent_fingerprint_mismatch_raises_conflict(self) -> None:
        ctx = self.ctx()
        key = "idem-fp-mismatch-001"
        content = {"name": "FP1", "description": "d", "audience": "a"}
        fp1 = service.compute_fingerprint({"entry_type": ENTRY_PRODUCT, "display_name": "FP1", "content": content})
        service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="FP1", content=content,
                             idempotency_key=key, request_fingerprint=fp1)
        fp2 = service.compute_fingerprint({"entry_type": ENTRY_PRODUCT, "display_name": "Different", "content": content})
        with self.assertRaises(service.IdempotencyConflict):
            service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Different", content=content,
                                 idempotency_key=key, request_fingerprint=fp2)

    def test_idempotent_cross_command_raises_conflict(self) -> None:
        ctx = self.ctx()
        key = "idem-cross-cmd-001"
        content = {"name": "Cross", "description": "d", "audience": "a"}
        fp = service.compute_fingerprint({"entry_type": ENTRY_PRODUCT, "display_name": "Cross", "content": content})
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="Cross", content=content,
                                     idempotency_key=key, request_fingerprint=fp)
        with self.assertRaises(service.IdempotencyConflict):
            service.save_draft(ctx, entry["id"], content, entry["draft"]["revision"],
                               idempotency_key=key, request_fingerprint=fp)

    def test_idempotent_replay_of_blocked_publish(self) -> None:
        """Blocked publish stores a terminal failure; retry replays the same error."""
        ctx = self.ctx(role=ROLE_OWNER)
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="BlockPub",
                                     content={"name": "x"})
        eid = entry["id"]
        rev = entry["draft"]["revision"]
        key = "idem-blocked-publish-001"
        fp = service.compute_fingerprint({"entry_id": eid, "expected_revision": rev, "acknowledged_warnings": []})
        with self.assertRaises(service.LibraryValidationError):
            service.publish(ctx, eid, rev, idempotency_key=key, request_fingerprint=fp)
        with self.assertRaises(service.LibraryValidationError):
            service.publish(ctx, eid, rev, idempotency_key=key, request_fingerprint=fp)

    def test_idempotent_replay_against_different_resource(self) -> None:
        """Same key+body against a different entry_id raises conflict due to resource-scoped fingerprint."""
        ctx = self.ctx()
        content_a = {"name": "A", "description": "d", "audience": "a"}
        content_b = {"name": "A", "description": "d", "audience": "a"}
        entry_a = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="A", content=content_a)
        entry_b = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="B", content=content_b)
        key = "idem-resource-scope-001"
        save_body = {"expected_revision": 1, "content": content_a}
        fp_a = service.compute_fingerprint({"entry_id": entry_a["id"], **save_body})
        service.save_draft(ctx, entry_a["id"], content_a, 1, idempotency_key=key, request_fingerprint=fp_a)
        fp_b = service.compute_fingerprint({"entry_id": entry_b["id"], **save_body})
        with self.assertRaises(service.IdempotencyConflict):
            service.save_draft(ctx, entry_b["id"], content_a, 1, idempotency_key=key, request_fingerprint=fp_b)

    def test_downstream_event_type_names_fit_column_width(self) -> None:
        for name in WITHDRAWAL_DOWNSTREAM_EVENT_TYPES.values():
            self.assertLessEqual(len(name), 100, f"Event type {name!r} exceeds column width")

    def test_idempotency_conflict_uses_stable_error_code(self) -> None:
        """IdempotencyConflict carries 'idempotency_key_reused' in detail, not generic 'library_conflict'."""
        ctx = self.ctx()
        key = "idem-stable-code-001"
        content = {"name": "SC", "description": "d", "audience": "a"}
        fp = service.compute_fingerprint({"entry_type": ENTRY_PRODUCT, "display_name": "SC", "content": content})
        service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="SC", content=content,
                             idempotency_key=key, request_fingerprint=fp)
        fp2 = service.compute_fingerprint({"entry_type": ENTRY_PRODUCT, "display_name": "SC2", "content": content})
        try:
            service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="SC2", content=content,
                                 idempotency_key=key, request_fingerprint=fp2)
            self.fail("Expected IdempotencyConflict")
        except service.IdempotencyConflict as exc:
            self.assertEqual(exc.detail.get("code"), "idempotency_key_reused")

    def test_reservation_survives_a_body_crash_and_retry_finishes_it(self) -> None:
        """A process failure after an external side effect must leave a durable claim."""
        ctx = self.ctx()
        key = "idem-durable-reservation-001"
        fingerprint = service.compute_fingerprint({"command": "durability-probe"})
        attempts = 0

        def crash_after_side_effect(_session, _key_hash):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("simulated process failure")

        with self.assertRaisesRegex(RuntimeError, "simulated process failure"):
            service._run_with_idempotency(
                ctx, key, "durability_probe", fingerprint, crash_after_side_effect,
            )

        with platform_session() as session:
            reservation = session.scalars(
                select(LibraryIdempotencyRecord).where(
                    LibraryIdempotencyRecord.workspace_id == ctx.workspace_id,
                    LibraryIdempotencyRecord.idempotency_key == key,
                )
            ).one()
            self.assertEqual(reservation.response_json, {"_state": "pending"})

        def finish(_session, _key_hash):
            nonlocal attempts
            attempts += 1
            return {"ok": True}

        self.assertEqual(
            service._run_with_idempotency(
                ctx, key, "durability_probe", fingerprint, finish,
            ),
            {"ok": True},
        )
        self.assertEqual(attempts, 2)

    def test_concurrent_first_use_executes_the_body_once(self) -> None:
        ctx = self.ctx()
        key = "idem-concurrent-first-use-001"
        fingerprint = service.compute_fingerprint({"command": "concurrent-probe"})
        start = threading.Barrier(2)
        release = threading.Event()
        entered = threading.Event()
        lock = threading.Lock()
        body_calls = 0
        results: list[dict] = []
        errors: list[BaseException] = []

        def body(_session, _key_hash):
            nonlocal body_calls
            with lock:
                body_calls += 1
            entered.set()
            release.wait(timeout=10)
            return {"winner": "cached"}

        def worker() -> None:
            try:
                start.wait(timeout=10)
                result = service._run_with_idempotency(
                    ctx, key, "concurrent_probe", fingerprint, body,
                )
                with lock:
                    results.append(result)
            except BaseException as exc:  # pragma: no cover - asserted below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(timeout=10), "one request must acquire the reservation")
        release.set()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "concurrent idempotency request hung")

        self.assertEqual(errors, [])
        self.assertEqual(body_calls, 1)
        self.assertEqual(results, [{"winner": "cached"}, {"winner": "cached"}])

    def test_key_without_a_request_fingerprint_is_rejected_before_reservation(self) -> None:
        ctx = self.ctx()
        with self.assertRaisesRegex(service.LibraryError, "request fingerprint"):
            service.create_entry(
                ctx,
                entry_type=ENTRY_PRODUCT,
                display_name="Missing fingerprint",
                idempotency_key="idem-missing-fingerprint-001",
            )
        with platform_session() as session:
            count = len(session.scalars(
                select(LibraryIdempotencyRecord).where(
                    LibraryIdempotencyRecord.workspace_id == ctx.workspace_id,
                )
            ).all())
        self.assertEqual(count, 0)

    def test_unknown_workspace_with_a_key_stays_a_domain_not_found_error(self) -> None:
        context = self.ctx(workspace="ws_idem_unknown", provision=False)
        fingerprint = service.compute_fingerprint({
            "entry_type": ENTRY_PRODUCT,
            "display_name": "Unknown workspace",
            "content": {},
        })
        with self.assertRaises(service.LibraryNotFound):
            service.create_entry(
                context,
                entry_type=ENTRY_PRODUCT,
                display_name="Unknown workspace",
                idempotency_key="idem-unknown-workspace-001",
                request_fingerprint=fingerprint,
            )

    def test_revision_conflict_replays_the_original_terminal_result(self) -> None:
        context = self.ctx()
        entry = service.create_entry(
            context,
            entry_type=ENTRY_PRODUCT,
            display_name="Cached revision conflict",
            content={"name": "base", "description": "v1", "audience": "a"},
        )
        entry_id = entry["id"]
        service.save_draft(
            context,
            entry_id,
            {"name": "winner", "description": "v2", "audience": "a"},
            1,
        )
        key = "idem-cached-revision-conflict-001"
        fingerprint = service.compute_fingerprint({
            "entry_id": entry_id,
            "expected_revision": 1,
            "content": {"name": "stale", "description": "v2", "audience": "a"},
        })
        with self.assertRaises(service.RevisionConflict) as first:
            service.save_draft(
                context,
                entry_id,
                {"name": "stale", "description": "v2", "audience": "a"},
                1,
                idempotency_key=key,
                request_fingerprint=fingerprint,
            )
        self.assertEqual(first.exception.detail["current_revision"], 2)

        service.save_draft(
            context,
            entry_id,
            {"name": "later", "description": "v3", "audience": "a"},
            2,
        )
        with self.assertRaises(service.RevisionConflict) as replay:
            service.save_draft(
                context,
                entry_id,
                {"name": "stale", "description": "v2", "audience": "a"},
                1,
                idempotency_key=key,
                request_fingerprint=fingerprint,
            )
        self.assertEqual(
            replay.exception.detail,
            first.exception.detail,
            "A retry must replay the original 409 rather than recompute current state",
        )

    def test_api_role_can_finalize_an_idempotency_reservation(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "deploy" / "postgres" / "domain-tenant-roles.sql"
        ).read_text(encoding="utf-8")
        grant = "GRANT UPDATE ON TABLE public.%I TO marketai_domain_api"
        grant_position = sql.index(grant)
        matrix_position = sql.rfind(
            "FOREACH relation_name IN ARRAY ARRAY[", 0, grant_position,
        )
        self.assertGreaterEqual(matrix_position, 0)
        self.assertIn(
            "'library_idempotency'",
            sql[matrix_position:grant_position],
            "The API must be able to finalize response_json after reserving a key",
        )


class LibraryConcurrencyTests(LibraryFixtures):
    """真正的并发：多个独立事务，由数据库而不是 Python 读值做仲裁。"""

    def _new_product_entry(self) -> tuple[TenantContext, str, int]:
        ctx = self.ctx()
        entry = service.create_entry(
            ctx,
            entry_type=ENTRY_PRODUCT,
            display_name="并发产品",
            content={"name": "base", "description": "v0", "audience": "a"},
        )
        return ctx, entry["id"], entry["draft"]["revision"]

    def test_a_committed_concurrent_writer_makes_the_stale_update_fail_at_the_database(self) -> None:
        ctx, entry_id, revision = self._new_product_entry()

        # 另一个已提交的事务先推进 revision（独立 session、独立连接）。
        with platform_session() as other:
            result = other.execute(
                update(LibraryDraftRecord)
                .where(
                    LibraryDraftRecord.entry_id == entry_id,
                    LibraryDraftRecord.revision == revision,
                )
                .values(revision=revision + 1, content_json={"name": "base", "description": "winner"})
            )
            self.assertEqual(getattr(result, "rowcount"), 1)

        with self.assertRaises(service.LibraryConflict) as caught:
            service.save_draft(ctx, entry_id, {"name": "base", "description": "loser"}, revision)
        detail = caught.exception.detail
        self.assertEqual(detail["current_revision"], revision + 1)
        self.assertEqual(detail["expected_revision"], revision)
        # 差异拿赢家已落库的内容比，用户才知道要合并什么。
        self.assertEqual(
            detail["field_diff"]["description"], {"base": "winner", "attempted": "loser"}
        )
        self.assertEqual(
            service.get_entry(ctx, entry_id)["draft"]["content"]["description"], "winner"
        )

    def _run_in_parallel(self, target, tags: tuple[str, ...]) -> list[tuple[str, str]]:
        """在同一个 barrier 上同时放行多个线程，各自开自己的事务。

        只接受两种结局：成功，或者一个带完整冲突信息的 ``LibraryConflict``。
        裸的 ``OperationalError``（SQLite/PostgreSQL 的锁或序列化错误）算测试
        失败——那说明服务层把数据库错误漏给了调用方，应该修服务层而不是让
        测试把它当成正常冲突吞掉。
        """
        outcomes: list[tuple[str, str, Any]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(len(tags))

        def worker(tag: str) -> None:
            barrier.wait(timeout=30)
            verdict: str
            detail: Any
            try:
                target(tag)
                verdict, detail = "ok", None
            except service.LibraryConflict as conflict:
                verdict, detail = "conflict", conflict.detail
            except OperationalError as exc:
                verdict, detail = "operational_error", exc
            except Exception as exc:  # noqa: BLE001 - 任何其它异常都要暴露出来
                verdict, detail = "unexpected", exc
            with lock:
                outcomes.append((verdict, tag, detail))

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in tags]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "并发线程超时未结束")

        self.assertEqual(len(outcomes), len(tags), f"有线程没有回报结果: {outcomes}")
        leaked = [(v, t, repr(d)) for v, t, d in outcomes if v in {"operational_error", "unexpected"}]
        self.assertEqual(
            leaked, [], f"服务层把数据库/未知异常漏给了调用方，必须修服务层: {leaked}"
        )
        for verdict, tag, detail in outcomes:
            if verdict == "conflict":
                # 冲突必须是可操作的，而不是一句"失败了"。
                self.assertIsInstance(detail, dict)
                self.assertIn("current_revision", detail, tag)
                self.assertIn("expected_revision", detail, tag)
                self.assertIn("field_diff", detail, tag)
        return [(verdict, tag) for verdict, tag, _detail in outcomes]

    def _assert_exactly_one_winner(self, outcomes: list[tuple[str, str]]) -> str:
        self.assertEqual(sorted(v for v, _ in outcomes), ["conflict", "ok"], outcomes)
        return next(tag for verdict, tag in outcomes if verdict == "ok")

    def test_two_simultaneous_save_draft_transactions_let_exactly_one_win(self) -> None:
        # 循环多轮：单次通过可能只是没撞上，重复运行才说明结果稳定。
        for round_no in range(8):
            with self.subTest(round=round_no):
                ctx, entry_id, revision = self._new_product_entry()

                def save(tag: str, _ctx=ctx, _id=entry_id, _rev=revision) -> None:
                    service.save_draft(_ctx, _id, {"name": "base", "description": tag}, _rev)

                outcomes = self._run_in_parallel(save, ("writer_a", "writer_b"))
                winner = self._assert_exactly_one_winner(outcomes)
                draft = service.get_entry(ctx, entry_id)["draft"]
                # 输家没有静默覆盖赢家，revision 只推进了一步。
                self.assertEqual(draft["content"]["description"], winner)
                self.assertEqual(draft["revision"], revision + 1)

    def test_two_simultaneous_binds_let_exactly_one_win(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        _ref_a, ref_a = self._reference_version(ctx, name="BindA")
        _ref_b, ref_b = self._reference_version(ctx, name="BindB", raw=png_bytes(colour=(21, 22, 23)))
        targets = {"binder_a": ref_a["id"], "binder_b": ref_b["id"]}

        for round_no in range(6):
            with self.subTest(round=round_no):
                entry = service.create_entry(
                    ctx, entry_type=ENTRY_PRODUCT, display_name=f"绑定并发{round_no}", content={}
                )
                entry_id, revision = entry["id"], entry["draft"]["revision"]

                def bind(tag: str, _id=entry_id, _rev=revision) -> None:
                    service.bind_reference(
                        ctx,
                        _id,
                        _rev,
                        reference_version_id=targets[tag],
                        purpose="hero",
                        viewpoint="front",
                    )

                outcomes = self._run_in_parallel(bind, ("binder_a", "binder_b"))
                winner = self._assert_exactly_one_winner(outcomes)
                draft = service.get_entry(ctx, entry_id)["draft"]
                bindings = draft["content"]["reference_bindings"]
                # 只有赢家的绑定被写入，输家没有追加第二条。
                self.assertEqual(len(bindings), 1, bindings)
                self.assertEqual(bindings[0]["reference_version_id"], targets[winner])
                self.assertEqual(draft["revision"], revision + 1)

    def test_two_simultaneous_publishes_produce_exactly_one_effective_version(self) -> None:
        ctx = self.ctx()
        _ref_entry, ref_version = self._reference_version(ctx)
        _group_entry, group_version = self._group_version(ctx, ref_version["id"])
        entry = service.create_entry(
            ctx,
            entry_type=ENTRY_PRODUCT,
            display_name="并发发布",
            content=self._product_content(ref_version["id"], group_version["id"]),
        )
        entry_id = entry["id"]
        revision = entry["draft"]["revision"]

        def publish(_tag: str) -> None:
            service.publish(ctx, entry_id, revision)

        outcomes = self._run_in_parallel(publish, ("publisher_a", "publisher_b"))
        self._assert_exactly_one_winner(outcomes)

        with platform_session() as session:
            versions = session.scalars(
                select(LibraryVersionRecord).where(LibraryVersionRecord.entry_id == entry_id)
            ).all()
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0].version_no, 1)
            self.assertEqual(len([v for v in versions if v.state == VERSION_EFFECTIVE]), 1)

    def test_four_simultaneous_publishes_still_produce_exactly_one_version(self) -> None:
        ctx = self.ctx()
        _ref_entry, ref_version = self._reference_version(ctx)
        _group_entry, group_version = self._group_version(ctx, ref_version["id"])
        for round_no in range(4):
            with self.subTest(round=round_no):
                entry = service.create_entry(
                    ctx,
                    entry_type=ENTRY_PRODUCT,
                    display_name=f"四路并发{round_no}",
                    content=self._product_content(ref_version["id"], group_version["id"]),
                )
                entry_id, revision = entry["id"], entry["draft"]["revision"]

                def publish(_tag: str, _id=entry_id, _rev=revision) -> None:
                    service.publish(ctx, _id, _rev)

                outcomes = self._run_in_parallel(publish, ("p1", "p2", "p3", "p4"))
                self.assertEqual(
                    sorted(v for v, _ in outcomes),
                    ["conflict", "conflict", "conflict", "ok"],
                    outcomes,
                )
                with platform_session() as session:
                    versions = session.scalars(
                        select(LibraryVersionRecord).where(
                            LibraryVersionRecord.entry_id == entry_id
                        )
                    ).all()
                    self.assertEqual(len(versions), 1)
                    self.assertEqual(
                        len([v for v in versions if v.state == VERSION_EFFECTIVE]), 1
                    )

    def test_database_refuses_a_second_effective_version_for_one_entry(self) -> None:
        """单 effective 版本是数据库约束，不只是服务层的先读后写。"""
        stack = self._full_stack()
        ctx = stack["ctx"]
        entry_id, version = stack["product"]

        with self.assertRaises(IntegrityError) as caught:
            with platform_session() as session:
                original = session.get(LibraryVersionRecord, version["id"])
                session.add(
                    LibraryVersionRecord(
                        id="libver_duplicate_effective",
                        workspace_id=ctx.workspace_id,
                        entry_id=entry_id,
                        entry_type=original.entry_type,
                        version_no=999,
                        content_json={"name": "绕过服务层"},
                        content_hash="0" * 64,
                        state=VERSION_EFFECTIVE,
                        eligibility=ELIGIBILITY_ELIGIBLE,
                        validation_json={},
                        published_by="attacker",
                    )
                )
        # 两种方言的报错措辞不同，都要接受：
        #   SQLite     -> UNIQUE constraint failed: library_versions.entry_id
        #   PostgreSQL -> duplicate key value violates unique constraint
        #                 "uq_library_version_single_effective"
        message = str(caught.exception)
        self.assertTrue(
            "library_versions.entry_id" in message
            or "uq_library_version_single_effective" in message,
            f"唯一约束报错措辞不认识，可能约束根本没生效: {message}",
        )
        self.assertEqual(len(service.get_entry(ctx, entry_id)["versions"]), 1)


class LibraryReferencePurgeTests(LibraryFixtures):
    def test_rejected_upload_original_is_purged_from_object_storage(self) -> None:
        ctx = self.ctx()
        with self.assertRaises(references.ReferenceGateError):
            service.upload_reference_object(
                ctx, raw=references.EICAR_SIGNATURE + b"\x00" * 32, declared_mime="image/png"
            )
        with platform_session() as session:
            record = session.scalars(
                select(ReferenceObjectRecord).where(
                    ReferenceObjectRecord.workspace_id == ctx.workspace_id
                )
            ).one()
            self.assertEqual(record.state, references.STATE_REJECTED)
            self.assertTrue(record.original_purged)
            self.assertEqual(record.original_object_key, "")
            # 证据保留：内容 hash 与检查结果仍可审计。
            self.assertEqual(len(record.content_sha256), 64)
            failed = [c for c in record.checks_json["checks"] if not c["passed"]]
            self.assertEqual([c["name"] for c in failed], ["malware_scan"])

    def test_reuploading_a_rejected_file_is_refused_without_touching_storage(self) -> None:
        ctx = self.ctx()
        payload = references.EICAR_SIGNATURE + b"\x00" * 32
        with self.assertRaises(references.ReferenceGateError):
            service.upload_reference_object(ctx, raw=payload, declared_mime="image/png")
        with self.assertRaises(references.ReferenceGateError) as caught:
            service.upload_reference_object(ctx, raw=payload, declared_mime="image/png")
        self.assertIn("不接受重复上传", str(caught.exception))
        with platform_session() as session:
            rows = session.scalars(
                select(ReferenceObjectRecord).where(
                    ReferenceObjectRecord.workspace_id == ctx.workspace_id
                )
            ).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].original_object_key, "")

    def test_released_originals_are_kept_private_and_not_purged(self) -> None:
        ctx = self.ctx()
        facts = service.upload_reference_object(
            ctx, raw=jpeg_with_exif(), declared_mime="image/jpeg"
        )
        with platform_session() as session:
            record = session.get(ReferenceObjectRecord, facts["object_id"])
            self.assertFalse(record.original_purged)
            self.assertTrue(Path(record.original_object_key).exists())

    def test_failed_purge_leaves_an_alertable_record_and_can_be_retried(self) -> None:
        ctx = self.ctx()
        original_purge = references.purge_original

        def failing_purge(record):
            # 模拟对象存储删除失败：文件仍在，记录必须如实反映滞留状态。
            payload = dict(record.checks_json or {})
            purge = dict(payload.get("purge") or {})
            purge["attempts"] = int(purge.get("attempts") or 0) + 1
            purge["last_error"] = "OSError: simulated storage failure"
            payload["purge"] = purge
            record.checks_json = payload
            record.original_purged = False
            return False, purge["last_error"]

        references.purge_original = failing_purge
        try:
            with self.assertRaises(references.ReferenceGateError):
                service.upload_reference_object(
                    ctx, raw=references.EICAR_SIGNATURE + b"\x01" * 16, declared_mime="image/png"
                )
        finally:
            references.purge_original = original_purge

        with platform_session() as session:
            record = session.scalars(
                select(ReferenceObjectRecord).where(
                    ReferenceObjectRecord.workspace_id == ctx.workspace_id
                )
            ).one()
            object_id = record.id
            leftover = record.original_object_key
            # 可查询、可告警的滞留条件，且原件确实还在磁盘上。
            self.assertEqual(record.state, references.STATE_REJECTED)
            self.assertFalse(record.original_purged)
            self.assertTrue(leftover)
            self.assertTrue(Path(leftover).exists())
            self.assertEqual(record.checks_json["purge"]["attempts"], 1)
            self.assertIn("simulated storage failure", record.checks_json["purge"]["last_error"])

        result = service.retry_rejected_original_purge(ctx)
        self.assertEqual(result["purged"], [object_id], result)
        self.assertEqual(result["still_pending"], [])
        self.assertFalse(Path(leftover).exists())

        with platform_session() as session:
            record = session.get(ReferenceObjectRecord, object_id)
            self.assertTrue(record.original_purged)
            self.assertEqual(record.original_object_key, "")
            audit = session.scalars(
                select(LibraryAuditRecord).where(
                    LibraryAuditRecord.workspace_id == ctx.workspace_id,
                    LibraryAuditRecord.command == "purge_rejected_original",
                )
            ).all()
            self.assertEqual([a.result for a in audit], ["ok"])

        # 已经清干净之后重试是空操作，不会重复计数或报错。
        self.assertEqual(service.retry_rejected_original_purge(ctx), {"purged": [], "still_pending": []})

    def test_purge_retry_that_keeps_failing_stays_auditable(self) -> None:
        ctx = self.ctx()
        original_purge = references.purge_original

        def failing_purge(record):
            payload = dict(record.checks_json or {})
            purge = dict(payload.get("purge") or {})
            purge["attempts"] = int(purge.get("attempts") or 0) + 1
            purge["last_error"] = "OSError: still failing"
            payload["purge"] = purge
            record.checks_json = payload
            record.original_purged = False
            return False, purge["last_error"]

        references.purge_original = failing_purge
        try:
            with self.assertRaises(references.ReferenceGateError):
                service.upload_reference_object(
                    ctx, raw=references.EICAR_SIGNATURE + b"\x02" * 16, declared_mime="image/png"
                )
            result = service.retry_rejected_original_purge(ctx)
        finally:
            references.purge_original = original_purge

        self.assertEqual(result["purged"], [])
        self.assertEqual(len(result["still_pending"]), 1)
        pending = result["still_pending"][0]
        self.assertEqual(pending["attempts"], 2)
        self.assertIn("still failing", pending["error"])

        with platform_session() as session:
            record = session.get(ReferenceObjectRecord, pending["object_id"])
            # 依然是 rejected + 未清除：运维可以按这个条件告警，文件没有失联。
            self.assertEqual(record.state, references.STATE_REJECTED)
            self.assertFalse(record.original_purged)
            self.assertTrue(Path(record.original_object_key).exists())
            blocked = session.scalars(
                select(LibraryAuditRecord).where(
                    LibraryAuditRecord.workspace_id == ctx.workspace_id,
                    LibraryAuditRecord.command == "purge_rejected_original",
                )
            ).all()
            self.assertEqual([a.result for a in blocked], ["blocked"])


class LibraryUploadAuditTests(LibraryFixtures):
    """每一条上传拒绝路径都必须先落审计再抛异常（评审项 1）。"""

    def _blocked_audit(self, ctx) -> list[LibraryAuditRecord]:
        with platform_session() as session:
            return list(
                session.scalars(
                    select(LibraryAuditRecord).where(
                        LibraryAuditRecord.workspace_id == ctx.workspace_id,
                        LibraryAuditRecord.command == "upload_reference",
                    )
                ).all()
            )

    def test_every_upload_rejection_path_persists_a_blocked_audit(self) -> None:
        good = png_bytes(colour=(31, 32, 33))
        cases: dict[str, dict] = {
            "empty_body": dict(raw=b"", declared_mime="image/png"),
            "oversized_body": dict(
                raw=b"x" * (config.LIBRARY_MAX_REFERENCE_BYTES + 1), declared_mime="image/png"
            ),
            "invalid_mime": dict(raw=good, declared_mime="application/pdf"),
            "gate_failure": dict(
                raw=references.EICAR_SIGNATURE + b"\x03" * 16, declared_mime="image/png"
            ),
        }
        for label, kwargs in cases.items():
            with self.subTest(path=label):
                ctx = self.ctx()
                with self.assertRaises(references.ReferenceGateError) as caught:
                    service.upload_reference_object(ctx, filename="probe.png", **kwargs)
                rows = self._blocked_audit(ctx)
                self.assertEqual(len(rows), 1, f"{label}: 期望恰好一条审计")
                row = rows[0]
                self.assertEqual(row.result, "blocked")
                self.assertEqual(row.initiator_id, ctx.initiator_id)
                # 原因落库，不是一句空的"失败了"。
                self.assertEqual(row.detail_json["reason"], str(caught.exception))
                self.assertEqual(row.detail_json["byte_size"], len(kwargs["raw"]))
                self.assertEqual(row.detail_json["declared_mime"], kwargs["declared_mime"])
                if kwargs["raw"]:
                    self.assertEqual(len(row.detail_json["content_sha256"]), 64)
                else:
                    self.assertEqual(row.detail_json["content_sha256"], "")

    def test_duplicate_upload_of_rejected_content_is_audited_too(self) -> None:
        ctx = self.ctx()
        payload = references.EICAR_SIGNATURE + b"\x04" * 16
        with self.assertRaises(references.ReferenceGateError):
            service.upload_reference_object(ctx, raw=payload, declared_mime="image/png")
        with self.assertRaises(references.ReferenceGateError) as caught:
            service.upload_reference_object(ctx, raw=payload, declared_mime="image/png")
        self.assertIn("不接受重复上传", str(caught.exception))

        rows = self._blocked_audit(ctx)
        # 第一次门禁失败 + 第二次重复上传，两条都在。
        self.assertEqual([r.result for r in rows], ["blocked", "blocked"])
        self.assertIn("不接受重复上传", rows[1].detail_json["reason"])
        # 进入 quarantine 之前就被拦下，所以没有 object_id，但有内容指纹可对账。
        self.assertIsNone(rows[1].detail_json["object_id"])
        self.assertEqual(rows[1].detail_json["content_sha256"], references.sha256_bytes(payload))

    def test_unknown_workspace_upload_writes_no_audit_row(self) -> None:
        ghost = self.ctx(workspace="ws_audit_ghost", provision=False)
        with self.assertRaises(service.LibraryNotFound):
            service.upload_reference_object(ghost, raw=png_bytes(), declared_mime="image/png")
        # 没有可归属的租户，审计行本身也会违反外键，所以这条路径故意不写审计。
        self.assertEqual(self._blocked_audit(ghost), [])


class LibraryWithdrawScopeTests(LibraryFixtures):
    def test_withdraw_propagation_is_limited_to_library_internal_dependencies(self) -> None:
        """把契约 §14 里尚未接入的下游对象钉死在测试里，避免把覆盖范围说大。

        DependencyIndex 目前只索引创意库版本→版本。Generation Draft/Run、
        AssetVersion、Experiment、LocalizationBatch、TruthPackage、
        MarketAssetBinding 和 Launch 仍然引用旧的 products 表，因此 Withdraw
        不会阻断它们。接入属于 #20/#21 及后续 ticket。
        """
        from app.adapters.persistence.database import Base

        stack = self._full_stack()
        owner = stack["owner"]
        _ref_entry, ref_version = stack["reference"]

        result = service.withdraw_version(owner, ref_version["id"], reason="范围说明用例")
        impacted_types = {item["entry_type"] for item in result["dependents"]}
        # 影响清单里只可能出现创意库条目类型，没有任何域外对象。
        self.assertTrue(impacted_types <= _LIBRARY_ENTRY_TYPES, impacted_types)

        # 扫描所有非创意库表：只要有人给下游加了指向 library_versions 的外键，
        # 这条断言就会失败——那正是"必须同步实现完整 §14 传播"的信号。
        library_tables = {
            "library_entries", "library_drafts", "library_versions", "library_dependencies",
            "library_events", "library_audit", "reference_objects", "reference_bindings",
            # Issue #22 intentionally references library_versions from experiment_versions;
            # withdraw propagation into the experiment domain is tracked separately.
            "experiment_versions",
            # Issue #24 (Localization, spec §1.3) intentionally references
            # library_versions from truth_packages, truth_source_attempts, and
            # fact_library_dependencies. Stale propagation into the truth
            # domain is a separate mechanism (an idempotent worker consuming
            # library_events + fact_library_dependencies per spec §1.3's
            # closing paragraph), not this DependencyIndex/withdraw_version
            # path -- tracked separately from #19's §14 scope.
            "truth_packages",
            "truth_source_attempts",
            "fact_library_dependencies",
            # Issue #24 (Localization, spec §1.5-§1.6) intentionally references
            # library_versions from localization_batches (product_version_id,
            # offer_version_id) and localized_copy_versions
            # (brand_localization_policy_version_id). Same rationale as the
            # truth-resolution tables above: propagation into the
            # Localization domain is a separate mechanism, not this
            # DependencyIndex/withdraw_version path.
            "localization_batches",
            "localized_copy_versions",
            # Issue #24 (Localization, spec §1.10) intentionally references
            # library_versions from market_asset_bindings (offer_version_id)
            # and localized_asset_lineages (product_version_id,
            # offer_version_id). Same rationale as the truth-resolution and
            # batch/copy tables above: propagation into the Localization
            # domain is a separate mechanism, not this
            # DependencyIndex/withdraw_version path.
            "market_asset_bindings",
            "localized_asset_lineages",
            # Issue #86 (Scale Enrollment lineage hardening, spec §4.3)
            # intentionally references library_versions from
            # scale_delivery_targets (offer_version_id,
            # landing_page_version_id) -- proving the exact library version
            # bound to a delivery slot is the point of that table. Same
            # rationale as the Experiment and Localization tables above:
            # Withdraw propagation into the Scale domain is a separate
            # mechanism, not this DependencyIndex/withdraw_version path.
            "scale_delivery_targets",
            # Issue #86 Task 8 (spec §7.3) intentionally references
            # library_versions twice from scale_lineage_guards
            # (product_version_id, offer_version_id) -- the guard freezes
            # the exact Product/Offer versions a Winner/DeliveryTarget pair
            # was validated against. Same rationale as scale_delivery_
            # targets above: Withdraw propagation into the Scale domain
            # remains a separate mechanism, not this DependencyIndex/
            # withdraw_version path.
            "scale_lineage_guards",
        }
        newly_wired = []
        for table in Base.metadata.tables.values():
            if table.name in library_tables:
                continue
            for column in table.columns:
                for fk in column.foreign_keys:
                    if fk.column.table.name == "library_versions":
                        newly_wired.append(f"{table.name}.{column.name}")
        self.assertEqual(
            newly_wired,
            [],
            "检测到下游表新增了指向 library_versions 的外键: "
            f"{newly_wired}。\n"
            "这意味着 §14 的下游对象已经开始引用创意库版本，"
            "Withdraw 传播必须同步扩展到它们（Generation Draft/Run、AssetVersion、"
            "Experiment、LocalizationBatch、TruthPackage、MarketAssetBinding、Launch），"
            "并把它们纳入 DependencyIndex 与影响分析。\n"
            "在实现之前不得删除本断言，也不得声称 #19 已完成 §14 传播。",
        )


class LibraryUploadIdempotencyTests(LibraryFixtures):
    """Upload terminal-failure replay via centralized idempotency protocol."""

    def _blocked_audit(self, ctx) -> list[LibraryAuditRecord]:
        with platform_session() as session:
            return list(
                session.scalars(
                    select(LibraryAuditRecord).where(
                        LibraryAuditRecord.workspace_id == ctx.workspace_id,
                        LibraryAuditRecord.command == "upload_reference",
                    )
                ).all()
            )

    def test_upload_rejected_replay_returns_cached_error(self) -> None:
        """A rejected upload stores a terminal failure; replay with the same key re-raises it."""
        ctx = self.ctx(role=ROLE_OWNER)
        payload = references.EICAR_SIGNATURE + b"\x05" * 16
        key = "idem-upload-reject-001"
        fp = service.compute_fingerprint({
            "content_sha256": service._hashlib.sha256(payload).hexdigest(),
            "filename": "bad.png",
            "declared_mime": "image/png",
        })
        with self.assertRaises(references.ReferenceGateError):
            service.upload_reference_object(
                ctx, raw=payload, declared_mime="image/png", filename="bad.png",
                idempotency_key=key, request_fingerprint=fp,
            )
        audit_after_first = self._blocked_audit(ctx)
        self.assertEqual(len(audit_after_first), 1)

        with self.assertRaises(references.ReferenceGateError):
            service.upload_reference_object(
                ctx, raw=payload, declared_mime="image/png", filename="bad.png",
                idempotency_key=key, request_fingerprint=fp,
            )
        audit_after_replay = self._blocked_audit(ctx)
        self.assertEqual(len(audit_after_replay), 1, "Replay must not create a second audit row")


class LibraryNoOpAuditTests(LibraryFixtures):
    """No-op withdraw and archive paths must still create audit records."""

    def test_noop_withdraw_creates_audit(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        ref_entry, ref_version = self._reference_version(ctx)
        service.withdraw_version(ctx, ref_version["id"], reason="first withdrawal")
        service.withdraw_version(ctx, ref_version["id"], reason="second call")
        with platform_session() as session:
            rows = list(session.scalars(
                select(LibraryAuditRecord).where(
                    LibraryAuditRecord.workspace_id == ctx.workspace_id,
                    LibraryAuditRecord.command == "withdraw",
                )
            ).all())
        results = [r.result for r in rows]
        self.assertIn("no_op", results, "No-op withdraw must create an audit record")

    def test_noop_archive_creates_audit(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="ArchNoop",
                                     content={"name": "A", "description": "d", "audience": "a"})
        eid = entry["id"]
        rev = entry["draft"]["revision"]
        service.archive_entry(ctx, eid, expected_revision=rev)
        new_rev = rev + 1
        service.archive_entry(ctx, eid, expected_revision=new_rev)
        with platform_session() as session:
            rows = list(session.scalars(
                select(LibraryAuditRecord).where(
                    LibraryAuditRecord.workspace_id == ctx.workspace_id,
                    LibraryAuditRecord.command == "archive",
                )
            ).all())
        results = [r.result for r in rows]
        self.assertIn("no_op", results, "No-op archive must create an audit record")


class LibraryProblemDetailsTests(LibraryFixtures):
    """HTTP error responses must follow application/problem+json with top-level fields."""

    def _app(self):
        from app.main import app
        return app

    @contextmanager
    def _authenticated_client(self):
        from fastapi.testclient import TestClient
        from app.platform.http_identity import request_tenant_context

        app = self._app()
        context = self.ctx()
        previous = app.dependency_overrides.get(request_tenant_context)

        app.dependency_overrides[request_tenant_context] = lambda: context
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client, context
        finally:
            if previous is None:
                app.dependency_overrides.pop(request_tenant_context, None)
            else:
                app.dependency_overrides[request_tenant_context] = previous

    def _assert_problem_shape(self, body, expected_status):
        self.assertTrue(body["type"].startswith("urn:marketai:error:"))
        self.assertIn("title", body)
        self.assertEqual(body["status"], expected_status)
        self.assertIsInstance(body["detail"], str)
        self.assertTrue(body["instance"])
        self.assertIn("code", body)
        self.assertTrue(body["trace_id"])
        self.assertIn("retryable", body)
        self.assertIn("field_errors", body)

    def test_problem_details_shape_on_not_found(self) -> None:
        with self._authenticated_client() as (client, _context):
            resp = client.get("/api/v1/library/entries/nonexistent")
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self._assert_problem_shape(body, resp.status_code)
        # `route.path` on a route nested two include_router() levels deep
        # (app -> v1_router -> library_v1_router) is relative to its own
        # sub-router, not the full mounted path -- pre-existing behavior of
        # `_request_problem`'s `instance` derivation (app/library/api.py),
        # unrelated to the #27 Task 9 router removal.
        self.assertEqual(body["instance"], "/library/entries/{entry_id}")
        self.assertEqual(resp.headers["X-Trace-Id"], body["trace_id"])
        self.assertEqual(resp.headers.get("content-type"), "application/problem+json")

    def test_auth_failure_produces_problem_details(self) -> None:
        from fastapi.testclient import TestClient
        client = TestClient(self._app(), raise_server_exceptions=False)
        resp = client.get("/api/v1/library/entries/x")
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self._assert_problem_shape(body, 401)
        self.assertEqual(resp.headers.get("content-type"), "application/problem+json")

    def test_missing_header_produces_problem_details_with_field_errors(self) -> None:
        with self._authenticated_client() as (client, _context):
            resp = client.post(
                "/api/v1/library/entries",
                json={"entry_type": "product", "display_name": "X"},
            )
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self._assert_problem_shape(body, resp.status_code)
        self.assertEqual(body["code"], "validation_error")
        self.assertTrue(len(body["field_errors"]) > 0, "field_errors must list the missing header")
        self.assertEqual(resp.headers.get("content-type"), "application/problem+json")

    def test_revision_conflict_returns_correct_code(self) -> None:
        ctx = self.ctx(role=ROLE_OWNER)
        entry = service.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name="RevConf")
        eid = entry["id"]
        service.save_draft(ctx, eid, {"name": "A", "description": "d", "audience": "a"},
                           entry["draft"]["revision"])
        with self.assertRaises(service.RevisionConflict) as caught:
            service.save_draft(ctx, eid, {"name": "B"}, entry["draft"]["revision"])
        self.assertIn("current_revision", caught.exception.detail)

    # NOTE (#27 Task 9): the four `test_oversized_upload_*` HTTP tests that
    # used to live here exercised `POST /api/library/reference-objects` --
    # the pre-BFF multipart route's own streaming byte-limit pre-check
    # (`app.library.api.upload_reference_object`, which bails before
    # buffering the body via `service.reject_oversized_upload` /
    # `UploadTooLarge`). That router is unmounted as of this task (all
    # Library HTTP traffic is served from `/api/v1/library/*`); the v1
    # `POST /library/reference-objects` route is a distinct, still-501
    # upload-intent-mediated command per `app/library/v1_router.py`'s module
    # docstring, not a drop-in replacement, so there is no HTTP path left to
    # retarget these tests at. The oversized-body *rejection* itself is
    # still covered at the domain layer -- see
    # `LibraryUploadAuditTests.test_every_upload_rejection_path_persists_a_blocked_audit`
    # (``oversized_body`` case), which calls `service.upload_reference_object`
    # directly and asserts the blocked-audit row.

    def test_unhandled_exception_returns_problem_details(self) -> None:
        from unittest.mock import patch

        with self._authenticated_client() as (client, _context):
            with patch.object(service, "list_entries", side_effect=RuntimeError("boom")):
                resp = client.get("/api/v1/library/entries")
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self._assert_problem_shape(body, 500)
        self.assertEqual(body["code"], "internal_error")
        self.assertEqual(resp.headers.get("content-type"), "application/problem+json")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
