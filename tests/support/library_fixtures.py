"""Shared Creative Library test fixtures (moved out of test_library_core.py in #20 Task 1).

``LibraryFixtures`` itself carries no test cases; concrete test classes across
the suite inherit from it rather than from each other. It owns the shared
temp-database bootstrap (migrate to head once per process) plus the
content-building helpers used to publish a full reference -> group -> rule ->
product -> offer -> mapping -> person stack.
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path

# 单独运行依赖本模块的测试文件时使用自己的临时库；在 discover 下 test_foundation
# 已先设好同样的键，setdefault 保证两者共用同一个引擎，不会出现"配置写了但引擎已
# 绑定"的假象。
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="marketai-library-"))
os.environ.setdefault("MARKETAI_TEST_ROOT", str(_TEST_ROOT))
os.environ.setdefault("MARKETAI_DATA_DIR", str(_TEST_ROOT / "data"))
os.environ.setdefault("MARKETAI_DATABASE_URL", f"sqlite:///{_TEST_ROOT / 'marketai.db'}")
os.environ.setdefault("MARKETAI_WORKSPACE_ID", "ws_test")

from decimal import Decimal

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from PIL import Image  # noqa: E402

from app import config  # noqa: E402
from app.adapters.persistence.database import platform_session, tenant_session  # noqa: E402
from app.platform.tenancy import TenantContext  # noqa: E402
from app.adapters.persistence.models import (  # noqa: E402
    AssetRecord,
    GenerationRunRecord,
    JobRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
    new_id,
    utcnow,
)
from app.library import service  # noqa: E402
from app.library.contracts import (  # noqa: E402
    ENTRY_MARKET_MAPPING,
    ENTRY_OFFER,
    ENTRY_PERSON,
    ENTRY_PRODUCT,
    ENTRY_REFERENCE_ASSET,
    ENTRY_REFERENCE_GROUP,
    ENTRY_SELECTION_RULE,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from datetime import date, timedelta

FUTURE = (date.today() + timedelta(days=365)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()


def png_bytes(size: tuple[int, int] = (80, 60), colour: tuple[int, int, int] = (12, 34, 56)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_with_exif(size: tuple[int, int] = (120, 90)) -> bytes:
    """安全 fixture：带 EXIF（含 GPS IFD）的 JPEG。"""
    image = Image.new("RGB", size, (200, 120, 40))
    exif = image.getexif()
    exif[0x010F] = "MarketAI-Test-Make"
    exif[0x0131] = "MarketAI-Test-Software"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (52.0, 22.0, 13.0)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class LibraryFixtures(unittest.TestCase):
    """共享夹具，本身不含用例；各测试类继承它而不是互相继承。"""

    workspace_seq = 0

    @classmethod
    def setUpClass(cls) -> None:
        # test_foundation 的 tearDownClass 会删掉共享的临时目录；这里重建后再升级到 head。
        url = config.DATABASE_URL
        if url.startswith("sqlite:///"):
            Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        command.upgrade(cfg, "head")

    @staticmethod
    def provision_workspace(workspace_id: str) -> None:
        """模拟控制面开好一个 Workspace。领域服务自己绝不会建租户。"""
        with platform_session() as session:
            if session.get(WorkspaceRecord, workspace_id) is None:
                session.add(WorkspaceRecord(id=workspace_id, name=workspace_id))

    def ctx(
        self,
        role: str = ROLE_MEMBER,
        *,
        workspace: str | None = None,
        actor: str = "user_a",
        provision: bool = True,
    ) -> TenantContext:
        if workspace is None:
            LibraryFixtures.workspace_seq += 1
            workspace = f"ws_lib_{LibraryFixtures.workspace_seq}"
        if provision:
            self.provision_workspace(workspace)
        return TenantContext(
            workspace_id=workspace, role=role, initiator_id=actor,
            request_id="test-req", trace_id="trace-test",
        )

    def assert_stored_widths(self, ctx: TenantContext | None = None) -> None:
        """Every String(N) value the Workspace stored fits PostgreSQL's varchar(N).

        SQLite ignores declared widths; call this at the end of an end-to-end
        flow (see ``tests/support/varchar_widths.py``).
        """
        from tests.support.varchar_widths import assert_varchar_widths

        context = ctx if ctx is not None else self.ctx
        if not isinstance(context, TenantContext):
            raise TypeError("assert_stored_widths needs a TenantContext (pass ctx=...)")
        with tenant_session(context) as session:
            assert_varchar_widths(session, context.workspace_id)

    def owner_ctx(self, ctx: TenantContext, actor: str = "owner_a") -> TenantContext:
        return TenantContext(
            workspace_id=ctx.workspace_id, role=ROLE_OWNER, initiator_id=actor,
            request_id="test-req", trace_id="trace-test",
        )

    # ------------------------------------------------------------------
    # Generation → Assets (#21) fixture helpers.
    #
    # Short-hand over ``ctx()``/``provision_workspace`` for tests that only
    # need a bare TenantContext plus the minimal Product/Plan/Asset anchor
    # rows required by generation_runs / asset_versions foreign keys.
    # ------------------------------------------------------------------

    def _make_context(
        self,
        workspace_id: str | None = None,
        *,
        role: str = ROLE_MEMBER,
        actor: str = "user_a",
    ) -> TenantContext:
        if workspace_id is None:
            LibraryFixtures.workspace_seq += 1
            workspace_id = f"ws_gen_{LibraryFixtures.workspace_seq}"
        self.provision_workspace(workspace_id)
        return TenantContext(
            workspace_id=workspace_id, role=role, initiator_id=actor,
            request_id="test-req", trace_id="trace-test",
        )

    def _product_id(self, ctx: TenantContext, session=None) -> str:
        # ``session`` is optional: pass the caller's open tenant_session
        # when one is already active (SQLite allows only one writer
        # transaction per file, so opening a second nested session while
        # the caller's session holds uncommitted writes deadlocks). With
        # no caller session, open a short-lived one of our own.
        def _insert(s) -> str:
            product = ProductRecord(
                id=new_id("prod"),
                workspace_id=ctx.workspace_id,
                name="Test Product",
            )
            s.add(product)
            s.flush()
            return product.id

        if session is not None:
            return _insert(session)
        with tenant_session(ctx) as s:
            return _insert(s)

    def _plan_id(self, ctx: TenantContext, session=None) -> str:
        def _insert(s, product_id: str) -> str:
            plan = PlanRecord(
                id=new_id("plan"),
                workspace_id=ctx.workspace_id,
                product_id=product_id,
                mode="full",
                cap=0,
                total_full=0,
                snapshot_hash="snap_" + new_id("h"),
            )
            s.add(plan)
            s.flush()
            return plan.id

        if session is not None:
            return _insert(session, self._product_id(ctx, session))
        with tenant_session(ctx) as s:
            return _insert(s, self._product_id(ctx, s))

    def _create_generation_run(self, ctx: TenantContext, session) -> str:
        run = GenerationRunRecord(
            id=new_id("gr"),
            workspace_id=ctx.workspace_id,
            product_id=self._product_id(ctx, session),
            plan_id=self._plan_id(ctx, session),
            provider="mock", model="mock-v1",
            aspect_ratio="1:1", image_size="1024x1024",
            variants=1, use_ref=False, status="running",
            total=1, done=0, failed=0,
            est_cost=Decimal("0"), currency="USD", revision=1,
        )
        session.add(run)
        session.flush()
        return run.id

    def _asset_id(self, ctx: TenantContext, session=None) -> str:
        def _insert(s) -> str:
            product_id = self._product_id(ctx, s)
            run = GenerationRunRecord(
                id=new_id("gr"),
                workspace_id=ctx.workspace_id,
                product_id=product_id,
                plan_id=self._plan_id(ctx, s),
                provider="mock", model="mock-v1",
                aspect_ratio="1:1", image_size="1024x1024",
                variants=1, use_ref=False, status="running",
                total=1, done=0, failed=0,
                est_cost=Decimal("0"), currency="USD", revision=1,
            )
            s.add(run)
            s.flush()
            asset = AssetRecord(
                id=new_id("asset"),
                workspace_id=ctx.workspace_id,
                product_id=product_id,
                run_id=run.id,
                combo_id="combo_1",
                combo_code="combo_1",
                tags_json={},
                filename="test.png",
                provider="mock",
                model="mock-v1",
                prompt="test prompt",
                fingerprint="fp_" + new_id("h"),
                status="succeeded",
            )
            s.add(asset)
            s.flush()
            return asset.id

        if session is not None:
            return _insert(session)
        with tenant_session(ctx) as s:
            return _insert(s)

    def _create_operation(self, ctx: TenantContext, session):
        from app.platform.operations import create_operation
        op = create_operation(session, command_type="generation.submit")
        return op.id

    def _create_job(self, ctx: TenantContext, session) -> str:
        job = JobRecord(
            id=new_id("job"),
            workspace_id=ctx.workspace_id,
            initiator_role="system",
            type="generation.run_item",
            resource_type="run_item",
            resource_id="test",
            payload_version=1,
            payload_json={},
            idempotency_key=new_id("ik"),
            state="queued",
        )
        session.add(job)
        session.flush()
        return job.id

    def _create_reservation(self, ctx: TenantContext, session) -> str:
        from app.entitlements.cost_models import SpendReservationRecord
        res = SpendReservationRecord(
            id=new_id("sr"),
            workspace_id=ctx.workspace_id,
            operation_id=self._create_operation(ctx, session),
            job_id=self._create_job(ctx, session),
            semantic_key_hash="skh_" + new_id("h"),
            provider="mock",
            execution_mode="mock",
            provider_currency="USD",
            maximum_provider_amount=Decimal("0"),
            budget_currency="USD",
            maximum_budget_amount=Decimal("0"),
            outstanding_budget_amount=Decimal("0"),
            entitlement_version=0,
            lease_sequence=0,
            guardrail_revision=0,
            platform_policy_version=0,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(res)
        session.flush()
        return res.id

    def _create_run_item(self, ctx: TenantContext, session) -> str:
        from app.generation.models import RunItemRecord

        run_id = self._create_generation_run(ctx, session)
        item = RunItemRecord(
            id=new_id("ri"),
            workspace_id=ctx.workspace_id,
            generation_run_id=run_id,
            ordinal=0,
            combination_fingerprint="fp_test",
            semantic_fingerprint="sf_test",
            execution_mode="mock",
            state="pending",
            revision=1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(item)
        session.flush()
        return item.id

    # ------------------------------------------------------------------
    # fixture helpers
    # ------------------------------------------------------------------

    def _publish(self, ctx: TenantContext, entry_type: str, display_name: str, content: dict) -> tuple[str, dict]:
        entry = service.create_entry(ctx, entry_type=entry_type, display_name=display_name, content=content)
        entry_id = entry["id"]
        revision = entry["draft"]["revision"]
        report = service.validate_draft(ctx, entry_id, expected_revision=revision)
        self.assertTrue(report["ok"], f"{entry_type} 校验未通过: {report['blocking']}")
        version = service.publish(ctx, entry_id, revision)
        return entry_id, version

    def _reference_version(self, ctx: TenantContext, *, name: str = "Ref", raw: bytes | None = None) -> tuple[str, dict]:
        payload = raw if raw is not None else png_bytes()
        is_jpeg = payload[:2] == b"\xff\xd8"
        facts = service.upload_reference_object(
            ctx,
            raw=payload,
            declared_mime="image/jpeg" if is_jpeg else "image/png",
            filename=f"{name}.jpg" if is_jpeg else f"{name}.png",
        )
        content = {
            "object_id": facts["object_id"],
            "source": "upload",
            "file": {k: facts[k] for k in ("content_sha256", "mime_type", "byte_size", "width", "height")},
            "rights": {
                "state": "granted",
                "scope": "global_paid_social",
                "declared_by": ctx.initiator_id,
                "valid_to": FUTURE,
            },
            "gate_checks": facts.get("checks", []),
            "derivative": facts.get("derivative", {}),
        }
        return self._publish(ctx, ENTRY_REFERENCE_ASSET, name, content)

    def _group_version(self, ctx: TenantContext, reference_version_id: str) -> tuple[str, dict]:
        return self._publish(
            ctx,
            ENTRY_REFERENCE_GROUP,
            "白底棚拍组",
            {"members": [{"reference_version_id": reference_version_id, "role": "white_studio"}]},
        )

    def _product_content(self, reference_version_id: str, group_version_id: str, **overrides) -> dict:
        content = {
            "name": "Alpha 充电器",
            "description": "65W 氮化镓充电器",
            "audience": "差旅办公人群",
            "selling_points": ["体积小", "多口"],
            "brand_constraints": "Logo 必须完整",
            "must_match": [
                {
                    "id": "mm_logo",
                    "type": "logo",
                    "description": "正面 Logo 完整可读",
                    "severity": "blocking",
                    "viewpoints": ["front"],
                    "qc_method": "human_review",
                    "evidence_reference_version_ids": [reference_version_id],
                    "confirmed": True,
                }
            ],
            "reference_bindings": [
                {
                    "reference_version_id": reference_version_id,
                    "purpose": "hero",
                    "viewpoint": "front",
                    "priority": 0,
                    "note": "主图",
                    "conditions": {},
                }
            ],
            "reference_group_version_ids": [group_version_id],
        }
        content.update(overrides)
        return content

    def _offer_content(self, product_version_id: str, **overrides) -> dict:
        content = {
            "product_version_id": product_version_id,
            "item": {"sku": "ALPHA-65W", "variant_label": "黑色", "quantity": 1, "bundle_components": []},
            "landing_page_url": "https://example.com/alpha",
            "market_scope": ["US", "DE"],
            "claim_constraints": ["不得声称医疗功效"],
            "valid_from": "2026-01-01",
            "valid_to": FUTURE,
        }
        content.update(overrides)
        return content

    def _person_content(self, reference_version_id: str, **overrides) -> dict:
        content = {
            "name": "Nova",
            "person_type": "fictional",
            "age_expression": "25-30",
            "wardrobe": "商务休闲",
            "temperament": "沉稳",
            "prompt_fragments": ["calm asian woman in business casual"],
            "rights": {"state": "granted", "scope": "虚构形象", "regions": ["US"], "channels": ["meta"]},
            "reference_bindings": [
                {
                    "reference_version_id": reference_version_id,
                    "purpose": "face",
                    "viewpoint": "front",
                    "priority": 0,
                }
            ],
        }
        content.update(overrides)
        return content

    def _full_stack(self, ctx: TenantContext | None = None, owner: TenantContext | None = None) -> dict:
        """发布 reference → group → rule → product → offer → mapping → person。"""
        ctx = ctx or self.ctx()
        owner = owner or TenantContext(
            workspace_id=ctx.workspace_id, role=ROLE_OWNER, initiator_id="owner_a",
            request_id="test-req", trace_id="trace-test",
        )
        ref_entry, ref_version = self._reference_version(ctx)
        group_entry, group_version = self._group_version(ctx, ref_version["id"])
        rule_entry, rule_version = self._publish(
            ctx,
            ENTRY_SELECTION_RULE,
            "默认选择规则",
            {"rules": [{"match": {"aspect_ratio": "1:1"}, "group_version_id": group_version["id"], "priority": 0}]},
        )
        product_content = self._product_content(
            ref_version["id"], group_version["id"], default_reference_selection_rule_version_id=rule_version["id"]
        )
        product_entry, product_version = self._publish(ctx, ENTRY_PRODUCT, "Alpha 充电器", product_content)
        offer_entry, offer_version = self._publish(
            owner, ENTRY_OFFER, "Alpha 单品", self._offer_content(product_version["id"])
        )
        # The mapping stays a draft: its data_source_version_id is opaque, so publish
        # would refuse it with dependency_not_found (#61). Create entry + draft only.
        mapping_content = {
            "offer_version_id": offer_version["id"],
            "data_source_version_id": "dsv_opaque_shop_1",
            "external_object": {"kind": "variant", "id": "gid://shopify/ProductVariant/42"},
            "market": "US",
            "execution_mode": "mock",
        }
        mapping_entry_result = service.create_entry(
            ctx, entry_type=ENTRY_MARKET_MAPPING, display_name="Alpha US 映射", content=mapping_content,
        )
        mapping_entry = mapping_entry_result["id"]
        mapping_version = None  # draft only, see above
        person_entry, person_version = self._publish(
            ctx, ENTRY_PERSON, "Nova", self._person_content(ref_version["id"])
        )
        return {
            "ctx": ctx,
            "owner": owner,
            "reference": (ref_entry, ref_version),
            "group": (group_entry, group_version),
            "rule": (rule_entry, rule_version),
            "product": (product_entry, product_version),
            "offer": (offer_entry, offer_version),
            "mapping": (mapping_entry, mapping_version),
            "person": (person_entry, person_version),
        }
