"""Issue #163 -- test plans end to end: API -> drafting Job -> runs.

Real HTTP routes (``TestClient``), the real Worker (``WorkerRuntime``) for
the drafting Job and the Mock generation Run items, the real Library
publish, generation draft, preview and Atomic Submission.

* Mock drafting: the three scales show the planner's counts; max cost is 0.
* ``sample`` run: an AI dimension template (``source: "ai"``) published
  server-side, a generation draft wired to it, a preview whose API calls are
  one per direction, a submitted Run; replaying the stage returns the Run.
* ``full`` after the sample Run: preview API calls = remaining combinations
  x variants; combinations the sample produced are excluded as
  ``reused_from_sample``.
* Revise: remove / edit / add directions re-plan and re-price; If-Match.
* Generation defaults with and without a bound OpenAI key; Owner-only write;
  a BYOK run freezes OpenAI into the Run and reserves the quoted cost.
* Negative tenant and permission paths; audit events for every command.
* Issue #337 froze ``revise`` and ``runs``, and #343 ``create``: each answers
  ``410`` to an Owner, a Member and another Workspace and leaves nothing
  behind. Tests that need a plan, a revised plan or a Run build it through the
  domain (``service.create_plan``, ``service.revise_plan``,
  ``runs.start_run``), never by inserting rows.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from decimal import Decimal
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs

from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AdTestPlanDirectionRecord,
    AdTestPlanRecord,
    AdTestPlanRunRecord,
    AuditEventRecord,
    GenerationRunRecord,
    JobRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    OperationRecord,
    ProductRecord,
    ResolutionPreviewRecord,
)
from app.entitlements.cost_models import SpendReservationRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import LocalKmsAdapter
from app.entitlements.models import EntitlementProjectionRecord
from app.generation.vlm_qc import qc_max_cost
from app.generation.models import (
    GenerationDraftRecord,
    RunItemAssetVersionBindingRecord,
    RunItemRecord,
)
from app.library import service as library
from app.plans import drafting
from app.plans import runs as plan_runs
from app.plans import service as plan_service
from app.plans.v1_router import TestPlanCreateRequest
from app.library.contracts import ENTRY_PRODUCT, ROLE_MEMBER, ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.providers.dispatch import ExternalCallCoordinator
from app.providers.llm_chat import LlmChatClient
from app.providers.llm_mock import MockLlmChatAdapter
from app.providers.openai_chat import OpenAIChatAdapter
from app.worker_runtime import WorkerRuntime

PROTECTOR = LocalKmsAdapter.local_only()
SELLING_POINTS = ["10 分钟快充", "IP65 防水", "App 远程预约"]


def _contains_key(value, keys: set[str]) -> bool:
    if isinstance(value, dict):
        return any(key in keys or _contains_key(item, keys) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_key(item, keys) for item in value)
    return False


class PlanFixtures(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context(role=ROLE_MEMBER, actor="member_a")
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        self.current = self.ctx
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.blob_root = tempfile.TemporaryDirectory(prefix="marketai-plans-")
        self.addCleanup(self.blob_root.cleanup)
        #: The governed llm_chat client the Worker drafts byok plans with
        #: (a fake OpenAI; see ``_use_fake_openai``).
        self.llm_client = None
        self.openai_requests: list = []
        self.openai_answers: list = []

    # -- helpers ----------------------------------------------------------

    def _headers(self, key: str | None = None, **extra) -> dict:
        return {"Idempotency-Key": key or f"ik-{uuid.uuid4().hex}", **extra}

    def _product(self, ctx: TenantContext | None = None, *, points: list[str] | None = None) -> str:
        ctx = ctx or self.ctx
        content = {
            "name": "VoltNest 充电桩",
            "description": "家用 7kW 交流充电桩",
            "audience": "有私家车位的电动车车主",
            "selling_points": list(points or SELLING_POINTS),
            "audiences": [
                {"who": "新能源车主", "why": "想在家里快速充电", "pain": "公共桩排队"},
                {"who": "别墅业主", "why": "车库改造", "pain": "布线麻烦"},
            ],
        }
        entry = library.create_entry(ctx, entry_type=ENTRY_PRODUCT, display_name=content["name"], content=content)
        revision = entry["draft"]["revision"]
        report = library.validate_draft(ctx, entry["id"], expected_revision=revision)
        self.assertTrue(report["ok"], report)
        library.publish(
            ctx, entry["id"], revision,
            acknowledged_warnings=[item["code"] for item in report.get("warnings") or []],
        )
        with tenant_session(ctx) as session:
            return session.scalars(
                select(ProductRecord.id).where(
                    ProductRecord.workspace_id == ctx.workspace_id,
                    ProductRecord.product_profile_entry_id == entry["id"],
                )
            ).one()

    def _create(self, body: dict, *, key: str | None = None, expect: int = 202) -> dict:
        """Admit a plan through the domain: the route answers 410 (#343). The
        body is read by the route's own model, so its defaults apply."""
        request = TestPlanCreateRequest.model_validate(body)
        return self._domain(lambda: plan_service.create_plan(
            self.current, product_id=request.product_id, goal=request.goal,
            audience_text=request.audience_text, audience_card_index=request.audience_card_index,
            scale=request.scale, seed_asset_version_ids=request.seed_asset_version_ids,
            idempotency_key=key or f"ik-{uuid.uuid4().hex}",
            from_learnings_snapshot_id=request.from_learnings_snapshot_id,
        ), expect)

    def _get(self, plan_id: str, *, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/test-plans/{plan_id}")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _domain(self, command, expect: int) -> dict:
        """Run a domain command as ``self.current``; a refusal comes back as
        its code and message (also as ``detail``), like the Problem Details
        the route used to send."""
        try:
            view = command()
        except plan_service.PlanProblem as exc:
            self.assertEqual(exc.status, expect, str(exc))
            return {"code": exc.code, "message": exc.message, "detail": exc.message}
        self.assertLess(expect, 300, f"expected {expect}, the command succeeded")
        return view

    def _revise(self, plan_id: str, body: dict, *, revision: int, expect: int = 200) -> dict:
        """Revise through the domain service: the route answers 410 (#337)."""
        return self._domain(lambda: plan_service.revise_plan(
            self.current, plan_id, expected_revision=revision,
            remove=body.get("remove", []), restore=body.get("restore", []),
            edit=body.get("edit", []), add=body.get("add", []), scale=body.get("scale"),
        ), expect)

    def _run(self, plan_id: str, stage: str, *, key: str | None = None, expect: int = 202) -> dict:
        """Start a stage through the domain: the route answers 410 (#337)."""
        return self._domain(lambda: plan_runs.start_run(
            self.current, plan_id, stage=stage, idempotency_key=key or f"ik-{uuid.uuid4().hex}",
        ), expect)

    def _drain(self) -> None:
        quarantine_foreign_jobs(self.ctx.workspace_id)
        runtime = WorkerRuntime(owner=f"plans-{uuid.uuid4().hex[:6]}")
        runtime.blob_store = LocalFileBlobStore(
            root=self.blob_root.name, public_base_url="http://blob.test", signing_secret=b"test-secret",
        )
        if self.llm_client is not None:
            runtime._llm_chat_client = self.llm_client
        for _ in range(200):
            if not asyncio.run(runtime.run_once()):
                break

    def _ready_plan(self, *, scale: str = "sample", product_id: str | None = None) -> dict:
        accepted = self._create({"product_id": product_id or self._product(), "scale": scale})
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "ready", plan.get("failure"))
        return plan

    def _audits(self, event_type: str) -> list[AuditEventRecord]:
        with tenant_session(self.ctx) as session:
            rows = list(session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.workspace_id == self.ctx.workspace_id,
                    AuditEventRecord.event_type == event_type,
                )
            ))
            for row in rows:
                session.expunge(row)
            return rows

    def _preview(self, preview_id: str) -> dict:
        with tenant_session(self.ctx) as session:
            return dict(session.get(ResolutionPreviewRecord, preview_id).snapshot_json)

    def _draft_record(self, draft_id: str) -> GenerationDraftRecord:
        with tenant_session(self.ctx) as session:
            draft = session.get(GenerationDraftRecord, draft_id)
            session.expunge(draft)
            return draft

    def _items(self, run_id: str) -> list[RunItemRecord]:
        with tenant_session(self.ctx) as session:
            rows = list(session.scalars(
                select(RunItemRecord).where(
                    RunItemRecord.workspace_id == self.ctx.workspace_id,
                    RunItemRecord.generation_run_id == run_id,
                )
            ))
            for row in rows:
                session.expunge(row)
            return rows

    def _bind_openai(self) -> None:
        owner = self.owner_ctx(self.ctx)
        with tenant_session(owner) as session:
            session.add(EntitlementProjectionRecord(
                workspace_id=self.ctx.workspace_id,
                entitlement_version=1, lease_sequence=1, projection_version=1,
                semantic_hash="hash_plans", resolved_entitlements_json={"features": ["generation"]},
            ))
        admin = CredentialAdmin(PROTECTOR)
        with tenant_session(owner) as session:
            meta = admin.create(
                session, owner, credential_key="OPENAI_API_KEY", provider="openai", plaintext=b"sk-ws-own",
            )
            admin.bind(
                session, owner, binding_scope="generation", provider="openai",
                credential_version_id=meta.credential_version_id,
            )

    def _mock_drafting(self):
        """Draft with the deterministic mock drafter even when a key is bound
        (for tests about runs and revisions, not about drafting)."""
        return patch.object(drafting, "resolve_plan_drafter", lambda mode, **_: drafting.MockPlanDrafter())

    def _use_fake_openai(self, *answers) -> None:
        """Draft byok plans against a fake OpenAI answering ``answers`` in turn."""
        self.openai_answers = list(answers)
        self.llm_client = fake_openai_llm_client(self.openai_requests, self.openai_answers)


def openai_completion(content: str):
    return httpx.Response(200, json={
        "id": "chatcmpl-plan",
        "model": "gpt-5-mini-2025-08-07",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3000, "completion_tokens": 1500},
    })


def fake_openai_llm_client(requests: list, answers: list) -> LlmChatClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answers.pop(0)

    return LlmChatClient(
        coordinator=ExternalCallCoordinator(tenant_session),
        session_factory=tenant_session,
        protector=PROTECTOR,
        adapters={
            "openai": OpenAIChatAdapter(
                base_url="https://openai.test/v1", timeout=5, transport=httpx.MockTransport(handler),
            ),
            "mock": MockLlmChatAdapter(),
        },
    )


class TestDrafting(PlanFixtures):
    def test_create_drafts_directions_and_prices_every_scale(self):
        product_id = self._product()
        accepted = self._create({"product_id": product_id})
        self.assertEqual(accepted["status"], "drafting")
        self.assertFalse(accepted["replayed"])
        self.assertTrue(accepted["operation_id"])

        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["goal"], "explore")
        self.assertEqual(plan["scale"], "sample")
        self.assertEqual(plan["audience"]["card_index"], 0)
        self.assertEqual(plan["audience"]["text"], "新能源车主：想在家里快速充电")
        self.assertEqual(len(plan["directions"]), 3)
        self.assertEqual([d["direction_id"] for d in plan["directions"]], ["dir-1", "dir-2", "dir-3"])
        first = plan["directions"][0]
        self.assertIn("10 分钟快充", first["title"])
        self.assertTrue(first["what_it_tests"] and first["scene_description"] and first["copy"]["headline"])
        self.assertEqual([f["key"] for f in first["factors"]], ["scene", "angle", "people"])
        self.assertEqual(first["combination_count"], 8)

        stages = plan["stages"]
        self.assertEqual(
            {scale: (s["combinations"], s["variants"], s["images"]) for scale, s in stages.items()},
            {"sample": (3, 1, 3), "small": (12, 2, 24), "full": (24, 2, 48)},
        )
        self.assertEqual(stages["full"]["untested"]["count"], 0)
        self.assertEqual(stages["sample"]["untested"]["count"], 21)
        self.assertEqual(stages["sample"]["max_cost"], {"amount": "0", "currency": "USD"})
        self.assertEqual(plan["selected_stage"]["scale"], "sample")
        self.assertEqual(plan["expansion"]["scale"], "full")
        self.assertEqual(plan["expansion"]["images"], 48)
        self.assertEqual(plan["defaults"]["execution_mode"], "mock")
        self.assertIn("mock_mode", plan["defaults"]["warnings"])
        self.assertEqual(plan["drafting"]["drafter"], "mock")

        with tenant_session(self.ctx) as session:
            operation = session.get(OperationRecord, accepted["operation_id"])
            self.assertEqual(operation.state, "succeeded")
            self.assertEqual(operation.result_resource_id, accepted["plan_id"])
            record = session.get(AdTestPlanRecord, accepted["plan_id"])
            directions = list(session.scalars(
                select(AdTestPlanDirectionRecord).where(AdTestPlanDirectionRecord.plan_id == record.id)
            ))
            # Provider-neutral (contract §5.1): no provider, model or prompt.
            forbidden = {"provider", "model", "prompt", "final_prompt", "execution_mode"}
            self.assertFalse(_contains_key(record.recipe_json, forbidden))
            for direction in directions:
                for column in ("copy_json", "factor_values_json", "fragments_json"):
                    self.assertFalse(_contains_key(getattr(direction, column), forbidden))
            self.assertIsNone(session.scalars(
                select(SpendReservationRecord).where(
                    SpendReservationRecord.workspace_id == self.ctx.workspace_id)
            ).first(), "mock drafting reserves nothing")
        self.assertEqual(len(self._audits("test_plan.created")), 1)
        self.assertEqual(len(self._audits("test_plan.drafted")), 1)
        self.assert_stored_widths()

    def test_idempotency_key_replays_and_conflicts_on_another_body(self):
        product_id = self._product()
        first = self._create({"product_id": product_id}, key="ik-plan-1")
        again = self._create({"product_id": product_id}, key="ik-plan-1")
        self.assertEqual(again["plan_id"], first["plan_id"])
        self.assertTrue(again["replayed"])
        conflict = self._create({"product_id": product_id, "scale": "full"}, key="ik-plan-1", expect=409)
        self.assertEqual(conflict["code"], "idempotency_key_reused")

    def test_audience_text_and_validation_refusals(self):
        product_id = self._product()
        plan = self._create({"product_id": product_id, "audience_text": "通勤族", "goal": "explore"})
        self._drain()
        self.assertEqual(self._get(plan["plan_id"])["audience"]["text"], "通勤族")
        refused = self._create({"product_id": product_id, "audience_card_index": 5}, expect=422)
        self.assertEqual(refused["code"], "audience_card_out_of_range")
        refused = self._create({"product_id": product_id, "goal": "optimize"}, expect=422)
        self.assertEqual(refused["code"], "seed_assets_required")
        refused = self._create(
            {"product_id": product_id, "goal": "optimize", "seed_asset_version_ids": ["av_missing"]}, expect=404,
        )
        self.assertEqual(refused["code"], "seed_asset_not_found")

    def test_an_unusable_draft_fails_the_plan_and_its_operation(self):
        class EmptyDrafter:
            name = "broken"

            async def draft_plan(self, product_version, goal, audience, scale, winners_losers):
                return drafting.PlanDraft(factors=[], directions=[])

        accepted = self._create({"product_id": self._product()})
        with patch.object(drafting, "resolve_plan_drafter", lambda mode, **_: EmptyDrafter()):
            self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["failure"]["code"], "draft_invalid")
        self.assertEqual(plan["directions"], [])
        self.assertEqual(plan["stages"], {})
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.get(OperationRecord, accepted["operation_id"]).state, "failed")
        refused = self._run(accepted["plan_id"], "sample", expect=409)
        self.assertEqual(refused["code"], "test_plan_not_ready")
        self.assertEqual(len(self._audits("test_plan.draft_failed")), 1)

    def test_list_filters_by_product(self):
        product_a = self._product()
        product_b = self._product()
        self._create({"product_id": product_a})
        self._create({"product_id": product_b})
        listed = self.client.get("/api/v1/test-plans", params={"product_id": product_a})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["product_id"] for item in listed.json()["items"]], [product_a])


class TestRuns(PlanFixtures):
    def test_list_finds_the_plan_behind_a_generation_run(self):
        plan = self._ready_plan()
        self._ready_plan()
        run = self._run(plan["plan_id"], "sample")

        listed = self.client.get("/api/v1/test-plans", params={"run_id": run["run_id"]})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["plan_id"] for item in listed.json()["items"]], [plan["plan_id"]])
        self.assertEqual(listed.json()["items"][0]["run_stages"], ["sample"])

        unknown = self.client.get("/api/v1/test-plans", params={"run_id": "gr_not_a_plan_run"})
        self.assertEqual(unknown.json()["items"], [])

        other = self._make_context(role=ROLE_OWNER, actor="owner_b")
        self.addCleanup(cancel_workspace_jobs, other.workspace_id)
        self.current = other
        foreign = self.client.get("/api/v1/test-plans", params={"run_id": run["run_id"]})
        self.assertEqual(foreign.status_code, 200, foreign.text)
        self.assertEqual(foreign.json()["items"], [])

    def test_sample_run_publishes_an_ai_template_and_submits_one_image_per_direction(self):
        plan = self._ready_plan()
        run = self._run(plan["plan_id"], "sample")

        self.assertEqual(run["status"], "submitted")
        self.assertFalse(run["replayed"])
        self.assertTrue(run["run_id"] and run["operation_id"])
        self.assertEqual(run["counts"]["combinations"], 3)
        self.assertEqual(run["counts"]["images"], 3)
        self.assertEqual(run["defaults"]["execution_mode"], "mock")

        with tenant_session(self.ctx) as session:
            entry = session.get(LibraryEntryRecord, run["template_entry_id"])
            self.assertEqual(entry.entry_type, "dimension_template")
            version = session.get(LibraryVersionRecord, run["template_version_id"])
            self.assertEqual(version.state, "effective")
            self.assertEqual(entry.current_version_id, version.id)
            content = version.content_json
            self.assertEqual(content["source"], "ai")
            self.assertEqual(content["generated_by"]["test_plan_id"], plan["plan_id"])
            self.assertEqual([d["id"] for d in content["dimensions"]], ["direction", "scene", "angle", "people"])
            self.assertEqual(len(content["dimensions"][1]["values"]), 4)
            # Each direction is bound to its two scenes: 3 directions x 2 others.
            self.assertEqual(len(content["constraints"]), 6)
            run_record = session.get(GenerationRunRecord, run["run_id"])
            self.assertEqual((run_record.provider, run_record.model, run_record.variants), ("mock", "mock-v1", 1))

        draft = self._draft_record(run["generation_draft_id"])
        self.assertEqual(draft.selectors_json, {"dimension_template_ids": [run["template_entry_id"]]})
        self.assertEqual(draft.combination_strategy_json["kind"], "full")
        self.assertEqual(draft.combination_strategy_json["test_plan"]["stage"], "sample")
        self.assertEqual(draft.format_json, {"aspect_ratio": "1:1", "variants_per_combination": 1})
        rules = draft.exclusions_json["rules"]
        self.assertEqual(len(rules), 21)
        self.assertEqual({rule["reason"] for rule in rules}, {"not_in_stage"})
        self.assertNotIn("invalid", draft.exclusions_json)

        projections = self._preview(run["preview_id"])["ui_projections"]
        self.assertEqual(projections["combination_count"], 3)
        self.assertEqual(projections["call_count"], 3)
        # Template constraints dropped 4 x 4 x 2 x... -> only the plan space is left.
        self.assertEqual(
            projections["strategy_applied"]["full_count"] - projections["combinations_dropped_by_constraints"], 24,
        )
        self.assertEqual(len(self._items(run["run_id"])), 3)

        replay = self._run(plan["plan_id"], "sample")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["run_id"], run["run_id"])
        with tenant_session(self.ctx) as session:
            runs = session.scalar(
                select(func.count()).select_from(GenerationRunRecord).where(
                    GenerationRunRecord.workspace_id == self.ctx.workspace_id,
                )
            )
            self.assertEqual(runs, 1)
        self.assertEqual(len(self._audits("test_plan.run_requested")), 1)
        self.assertEqual(len(self._audits("test_plan.run_submitted")), 1)

        detail = self._get(plan["plan_id"])
        self.assertEqual(detail["runs"][0]["run_id"], run["run_id"])
        self.assertEqual(detail["runs"][0]["run_status"], "running")
        self.assert_stored_widths()

    def test_full_after_sample_generates_only_the_remaining_combinations(self):
        plan = self._ready_plan()
        sample = self._run(plan["plan_id"], "sample")

        waiting = self._run(plan["plan_id"], "full", expect=409)
        self.assertEqual(waiting["code"], "sample_run_in_progress")
        self.assertTrue(self._get(plan["plan_id"])["expansion"]["sample_run_in_progress"])

        self._drain()
        self.assertTrue(all(item.state == "succeeded" for item in self._items(sample["run_id"])))
        expansion = self._get(plan["plan_id"])["expansion"]
        self.assertEqual(
            (expansion["combinations"], expansion["reused_combinations"], expansion["variants"], expansion["images"]),
            (21, 3, 2, 42),
        )

        full = self._run(plan["plan_id"], "full")
        self.assertEqual(full["target_scale"], "full")
        self.assertEqual(full["counts"]["combinations"], 21)
        self.assertEqual(full["counts"]["reused_combinations"], 3)
        self.assertEqual(full["template_entry_id"], sample["template_entry_id"])
        self.assertEqual(full["template_version_id"], sample["template_version_id"], "unchanged plan -> same version")
        projections = self._preview(full["preview_id"])["ui_projections"]
        self.assertEqual(projections["combination_count"], 21)
        self.assertEqual(projections["call_count"], 21 * 2)
        reasons = [rule["reason"] for rule in self._draft_record(full["generation_draft_id"]).exclusions_json["rules"]]
        self.assertEqual(reasons.count("reused_from_sample"), 3)
        self.assertEqual(reasons.count("not_in_stage"), 0)
        sample_signatures = {item.stable_dimension_signature for item in self._items(sample["run_id"])}
        full_signatures = {item.stable_dimension_signature for item in self._items(full["run_id"])}
        self.assertEqual(len(full_signatures), 21)
        self.assertFalse(sample_signatures & full_signatures, "a sampled combination is never paid for twice")
        self.assertEqual(len(self._items(full["run_id"])), 42)

        self.assertEqual(self._run(plan["plan_id"], "full")["run_id"], full["run_id"])
        locked = self._revise(plan["plan_id"], {"remove": ["dir-1"]}, revision=plan["revision"], expect=409)
        self.assertEqual(locked["code"], "test_plan_locked")
        self.assert_stored_widths()

    def test_an_optimize_plan_carries_its_seed_assets_and_their_tags(self):
        product_id = self._product()
        plan = self._ready_plan(product_id=product_id)
        sample = self._run(plan["plan_id"], "sample")
        self._drain()
        with tenant_session(self.ctx) as session:
            seeds = list(session.scalars(
                select(RunItemAssetVersionBindingRecord.asset_version_id)
                .join(RunItemRecord, RunItemRecord.id == RunItemAssetVersionBindingRecord.run_item_id)
                .where(RunItemRecord.generation_run_id == sample["run_id"])
            ))[:2]
        self.assertEqual(len(seeds), 2)
        accepted = self._create({"product_id": product_id, "goal": "optimize", "seed_asset_version_ids": seeds})
        self._drain()
        optimize = self._get(accepted["plan_id"])
        self.assertEqual(optimize["status"], "ready")
        self.assertEqual(optimize["seed_asset_version_ids"], seeds)
        self.assertIn("以已选的 2 个素材为基线", optimize["directions"][0]["hypothesis"])
        with tenant_session(self.ctx) as session:
            winners = session.get(AdTestPlanRecord, accepted["plan_id"]).recipe_json["seeds"]["winners"]
        self.assertEqual([item["asset_version_id"] for item in winners], seeds)
        self.assertTrue(all(item["dimension_tags"] for item in winners))

    def test_full_without_a_sample_follows_the_plan_scale(self):
        plan = self._ready_plan(scale="small")
        self.assertEqual(plan["expansion"]["scale"], "small")
        full = self._run(plan["plan_id"], "full")
        self.assertEqual(full["target_scale"], "small")
        self.assertEqual(full["counts"], {
            "combinations": 12, "reused_combinations": 0, "variants": 2, "images": 24,
            "max_cost": {"amount": "0", "currency": "USD"},
        })
        self.assertEqual(self._preview(full["preview_id"])["ui_projections"]["call_count"], 24)

    def test_removing_a_direction_after_samples_shrinks_the_expansion(self):
        plan = self._ready_plan()
        self._run(plan["plan_id"], "sample")
        self._drain()
        revised = self._revise(plan["plan_id"], {"remove": ["dir-3"]}, revision=plan["revision"])
        self.assertEqual(revised["expansion"]["combinations"], 16 - 2)
        self.assertEqual(revised["expansion"]["reused_combinations"], 2)
        full = self._run(plan["plan_id"], "full")
        self.assertEqual(self._preview(full["preview_id"])["ui_projections"]["call_count"], 14 * 2)
        self.assertNotEqual(full["template_version_id"], self._get(plan["plan_id"])["runs"][0]["template_version_id"])


class TestRevise(PlanFixtures):
    def test_revise_removes_edits_and_adds_directions_and_reprices(self):
        self._bind_openai()
        with self._mock_drafting():
            plan = self._ready_plan()
        self.assertEqual(plan["defaults"]["execution_mode"], "byok")
        self.assertEqual(plan["defaults"]["model"], "gpt-image-2")
        # One image plus its quality check (Issue #169; no references bound).
        unit = Decimal("0.053") + qc_max_cost("openai", "gpt-5-mini", 1)
        self.assertEqual(Decimal(plan["stages"]["full"]["max_cost"]["amount"]), unit * 48)

        revised = self._revise(plan["plan_id"], {
            "remove": ["dir-2"],
            "edit": [{
                "direction_id": "dir-1",
                "title": "十分钟充满 80%",
                "copy": {"headline": "午饭时间就充好"},
                "factor_values": {"people": [{"value_id": "people-1"}], "scene": [{"label": "地下车库"}]},
            }],
            "add": [{"title": "冬季续航焦虑", "scene_description": "雪夜停车场里充电"}],
        }, revision=plan["revision"])

        self.assertEqual(revised["revision"], plan["revision"] + 1)
        directions = {d["direction_id"]: d for d in revised["directions"]}
        self.assertTrue(directions["dir-2"]["removed"])
        self.assertEqual(directions["dir-1"]["title"], "十分钟充满 80%")
        self.assertEqual(directions["dir-1"]["copy"]["headline"], "午饭时间就充好")
        self.assertEqual(directions["dir-1"]["factor_values"]["scene"], ["scene-5"])
        self.assertEqual(directions["dir-1"]["combination_count"], 2)  # 1 scene x 2 angles x 1 people
        self.assertTrue(directions["dir-4"]["user_added"])
        self.assertEqual(directions["dir-4"]["combination_count"], 1)  # first value of each factor
        scene = next(f for f in revised["factors"] if f["key"] == "scene")
        self.assertEqual(scene["values"][-1], {"id": "scene-5", "label": "地下车库", "proposed_by": "user"})

        # dir-1: 2, dir-3: 8, dir-4: 1 -> 11 combinations.
        self.assertEqual(revised["stages"]["full"]["combinations"], 11)
        self.assertEqual(revised["stages"]["sample"]["combinations"], 3)
        self.assertEqual(Decimal(revised["stages"]["full"]["max_cost"]["amount"]), unit * 22)
        self.assertEqual(Decimal(revised["stages"]["sample"]["max_cost"]["amount"]), unit * 3)
        audits = self._audits("test_plan.revised")
        self.assertEqual(len(audits), 1)
        self.assert_stored_widths()

        stale = self._revise(plan["plan_id"], {"remove": ["dir-1"]}, revision=plan["revision"], expect=409)
        self.assertEqual(stale["code"], "revision_conflict")

    def test_revise_refuses_removing_every_direction_and_unknown_ids(self):
        plan = self._ready_plan()
        refused = self._revise(
            plan["plan_id"], {"remove": ["dir-1", "dir-2", "dir-3"]}, revision=plan["revision"], expect=422,
        )
        self.assertEqual(refused["code"], "no_active_directions")
        missing = self._revise(plan["plan_id"], {"remove": ["dir-9"]}, revision=plan["revision"], expect=404)
        self.assertEqual(missing["code"], "direction_not_found")
        unknown = self._revise(plan["plan_id"], {
            "edit": [{"direction_id": "dir-1", "factor_values": {"lighting": [{"label": "逆光"}]}}],
        }, revision=plan["revision"], expect=422)
        self.assertEqual(unknown["code"], "unknown_factor")
        scale = self._revise(plan["plan_id"], {"scale": "full"}, revision=plan["revision"])
        self.assertEqual(scale["scale"], "full")
        self.assertEqual(scale["selected_stage"]["scale"], "full")


class TestDefaults(PlanFixtures):
    def test_defaults_without_a_key_are_mock_and_with_a_key_byok_openai(self):
        response = self.client.get("/api/v1/generation/defaults")
        self.assertEqual(response.status_code, 200, response.text)
        view = response.json()
        self.assertEqual(
            (view["provider"], view["model"], view["execution_mode"], view["aspect_ratio"], view["variants"]),
            ("mock", "mock-v1", "mock", "1:1", {"sample": 1, "small": 2, "full": 2}),
        )
        self.assertEqual((view["source"], view["revision"], view["credential_bound"]), ("computed", 0, False))
        self.assertEqual(response.headers.get("etag"), '"0"')

        self._bind_openai()
        view = self.client.get("/api/v1/generation/defaults").json()
        self.assertEqual(
            (view["provider"], view["model"], view["execution_mode"], view["credential_bound"]),
            ("openai", "gpt-image-2", "byok", True),
        )

    def test_only_an_owner_saves_defaults_and_runs_freeze_them(self):
        body = {
            "provider": "mock", "model": "mock-v1", "execution_mode": "mock",
            "aspect_ratio": "9:16", "variants": {"sample": 2, "small": 3, "full": 4},
        }
        forbidden = self.client.put(
            "/api/v1/generation/defaults", json=body, headers=self._headers(**{"If-Match": '"0"'}),
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        self.assertEqual(forbidden.json()["code"], "generation_defaults_owner_only")

        self.current = self.owner_ctx(self.ctx)
        saved = self.client.put(
            "/api/v1/generation/defaults", json=body, headers=self._headers(**{"If-Match": '"0"'}),
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["source"], saved.json()["revision"]), ("workspace", 1))
        stale = self.client.put(
            "/api/v1/generation/defaults", json=body, headers=self._headers(**{"If-Match": '"0"'}),
        )
        self.assertEqual(stale.status_code, 409)
        invalid = self.client.put(
            "/api/v1/generation/defaults",
            json={**body, "aspect_ratio": "5:7"}, headers=self._headers(**{"If-Match": '"1"'}),
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "aspect_ratio_unsupported")
        self.assertEqual(len(self._audits("generation_defaults.updated")), 1)

        self.current = self.ctx
        plan = self._ready_plan()
        self.assertEqual(plan["stages"]["sample"]["images"], 3 * 2)
        run = self._run(plan["plan_id"], "sample")
        self.assertEqual(run["defaults"]["aspect_ratio"], "9:16")
        self.assertEqual(
            self._draft_record(run["generation_draft_id"]).format_json,
            {"aspect_ratio": "9:16", "variants_per_combination": 2},
        )
        self.assertEqual(len(self._items(run["run_id"])), 6)

    def test_a_byok_run_binds_openai_and_reserves_the_quoted_cost(self):
        self._bind_openai()
        with self._mock_drafting():
            plan = self._ready_plan()
        run = self._run(plan["plan_id"], "sample")
        self.assertEqual(run["defaults"]["execution_mode"], "byok")
        # Three images, each with its quality check (Issue #169): the card,
        # the preview and the reservation agree to the micro-dollar.
        expected = format((Decimal("0.053") + qc_max_cost("openai", "gpt-5-mini", 1)) * 3, "f")
        self.assertEqual(run["counts"]["max_cost"], {"amount": expected, "currency": "USD"})
        with tenant_session(self.ctx) as session:
            record = session.get(GenerationRunRecord, run["run_id"])
            self.assertEqual((record.provider, record.model, record.execution_mode), ("openai", "gpt-image-2", "byok"))
            self.assertEqual(record.est_cost, Decimal(expected))
        projections = self._preview(run["preview_id"])["ui_projections"]
        self.assertEqual(projections["maximum_cost"], {"amount": expected, "currency": "USD"})
        self.assert_stored_widths()


class TestTenancyAndPermissions(PlanFixtures):
    def test_another_workspaces_plan_and_product_are_not_found(self):
        plan = self._ready_plan()
        other = self._make_context(role=ROLE_OWNER, actor="owner_b")
        self.addCleanup(cancel_workspace_jobs, other.workspace_id)
        other_product = self._product(other)

        self.current = other
        self.assertEqual(self._get(plan["plan_id"], expect=404)["code"], "test_plan_not_found")
        # The domain commands behind the frozen routes refuse it the same way.
        for refused in (
            self._revise(plan["plan_id"], {"remove": ["dir-1"]}, revision=plan["revision"], expect=404),
            self._run(plan["plan_id"], "sample", expect=404),
        ):
            self.assertEqual(refused["code"], "test_plan_not_found")
        self.assertEqual(self.client.get("/api/v1/test-plans").json()["items"], [])

        self.current = self.ctx
        refused = self._create({"product_id": other_product}, expect=404)
        self.assertEqual(refused["code"], "product_not_found")

    def test_members_and_owners_both_create_and_the_domain_revises_and_runs(self):
        owner = self.owner_ctx(self.ctx)
        self.current = owner
        plan = self._ready_plan()
        self.current = self.ctx  # a Member revises and runs the Owner's plan (domain)
        revised = self._revise(plan["plan_id"], {"remove": ["dir-3"]}, revision=plan["revision"])
        run = self._run(plan["plan_id"], "sample")
        self.assertEqual(run["counts"]["combinations"], 2)
        self.assertEqual(revised["revision"], 2)

    def test_an_unpublished_product_is_refused(self):
        entry = library.create_entry(
            self.ctx, entry_type=ENTRY_PRODUCT, display_name="草稿产品",
            content={"name": "草稿产品", "description": "d", "audience": "a"},
        )
        with tenant_session(self.ctx) as session:
            product_id = session.scalars(
                select(ProductRecord.id).where(ProductRecord.product_profile_entry_id == entry["id"])
            ).first()
        refused = self._create({"product_id": product_id or entry["id"]}, expect=409)
        self.assertEqual(refused["code"], "product_not_published")


#: Every table a legacy command wrote to, directly or through Generation,
#: Library, Jobs, cost or audit.
FOOTPRINT = (
    AdTestPlanRecord, AdTestPlanDirectionRecord, AdTestPlanRunRecord, GenerationRunRecord, JobRecord,
    OperationRecord, SpendReservationRecord, AuditEventRecord, LibraryEntryRecord,
)


class FrozenCommandFixtures(PlanFixtures):
    def _router_logger(self) -> logging.Logger:
        """The router's logger, switched back on: the Alembic migration in
        ``LibraryFixtures.setUpClass`` runs ``fileConfig`` with
        ``disable_existing_loggers`` (see ``tests.test_asset_thumbnails``)."""
        logger = logging.getLogger("app.plans.v1_router")
        self.addCleanup(setattr, logger, "disabled", logger.disabled)
        logger.disabled = False
        return logger

    #: The commands ``_post_frozen`` sends, in order (the log names each).
    FROZEN_CALLS = ("create_test_plan",) * 3 + ("revise_test_plan",) * 2 + ("create_test_plan_run",) * 2

    def _post_frozen(self, plan_id: str, product_id: str) -> list:
        """Every frozen command as ``self.current``: ``create`` twice with one
        Idempotency-Key (the replay is frozen too) and once with another body,
        then ``revise`` and ``runs`` twice each."""
        replayed = self._headers()
        return [
            self.client.post("/api/v1/test-plans", json={"product_id": product_id}, headers=replayed),
            self.client.post("/api/v1/test-plans", json={"product_id": product_id}, headers=replayed),
            self.client.post(
                "/api/v1/test-plans", json={"product_id": product_id, "scale": "full", "audience_text": "通勤族"},
                headers=self._headers(),
            ),
            self.client.post(
                f"/api/v1/test-plans/{plan_id}/revise", json={"remove": ["dir-1"]},
                headers=self._headers(**{"If-Match": '"1"'}),
            ),
            # Without If-Match: still 410, never 428.
            self.client.post(f"/api/v1/test-plans/{plan_id}/revise", json={"scale": "full"}, headers=self._headers()),
            self.client.post(f"/api/v1/test-plans/{plan_id}/runs", json={"stage": "sample"}, headers=self._headers()),
            self.client.post(f"/api/v1/test-plans/{plan_id}/runs", json={"stage": "full"}, headers=self._headers()),
        ]

    def assert_frozen(self, responses: list) -> None:
        for response in responses:
            self.assertEqual(response.status_code, 410, response.text)
            self.assertEqual(response.headers["content-type"], "application/problem+json")
            problem = response.json()
            self.assertEqual((problem["status"], problem["code"], problem["retryable"]),
                             (410, "legacy_test_plans_frozen", False))

    def _footprint(self, *contexts: TenantContext) -> dict:
        """Row counts per Workspace and table, plus every plan's and
        direction's state, so a command that wrote anything shows up."""
        footprint: dict = {}
        for ctx in contexts:
            with tenant_session(ctx) as session:
                for model in FOOTPRINT:
                    footprint[(ctx.workspace_id, model.__tablename__)] = session.scalar(
                        select(func.count()).select_from(model).where(model.workspace_id == ctx.workspace_id)
                    )
                footprint[(ctx.workspace_id, "plans")] = sorted(session.execute(
                    select(AdTestPlanRecord.id, AdTestPlanRecord.revision, AdTestPlanRecord.status,
                           AdTestPlanRecord.scale, AdTestPlanRecord.updated_at)
                    .where(AdTestPlanRecord.workspace_id == ctx.workspace_id)
                ).all())
                footprint[(ctx.workspace_id, "directions")] = sorted(session.execute(
                    select(AdTestPlanDirectionRecord.id, AdTestPlanDirectionRecord.removed,
                           AdTestPlanDirectionRecord.updated_at)
                    .where(AdTestPlanDirectionRecord.workspace_id == ctx.workspace_id)
                ).all())
        return footprint


class TestFrozenCommands(FrozenCommandFixtures):
    """#337 / #343: ``create``, ``revise`` and ``runs`` answer ``410
    legacy_test_plans_frozen`` to an Owner, a Member and another Workspace
    without reading the product or plan -- no plan, direction, run,
    Generation, Job, operation, reservation, Library or audit row -- and log
    one structured line each. Reads are unchanged."""

    def test_a_member_and_an_owner_get_410_and_nothing_is_written(self):
        plan = self._ready_plan()
        self._bind_openai()  # a byok create would have reserved its drafting call
        before = self._footprint(self.ctx)

        for ctx in (self.ctx, self.owner_ctx(self.ctx)):
            self.current = ctx
            with self.assertLogs(self._router_logger(), level="WARNING") as logs:
                responses = self._post_frozen(plan["plan_id"], plan["product_id"])
            self.assert_frozen(responses)
            self.assertEqual(
                [(r.getMessage(), r.operation_id, r.workspace_id, r.plan_id) for r in logs.records],
                [("legacy_test_plans_frozen", operation, ctx.workspace_id,
                  None if operation == "create_test_plan" else plan["plan_id"])
                 for operation in self.FROZEN_CALLS],
            )

        self.assertEqual(self._footprint(self.ctx), before)
        detail = self._get(plan["plan_id"])
        self.assertEqual((detail["status"], detail["revision"], detail["runs"]), ("ready", plan["revision"], []))
        self.assertEqual(detail["directions"], plan["directions"])
        results = self.client.get(f"/api/v1/test-plans/{plan['plan_id']}/results")
        self.assertEqual(results.status_code, 200, results.text)

    def test_another_workspace_and_an_unknown_plan_get_410_not_404(self):
        plan = self._ready_plan()
        other = self._make_context(role=ROLE_OWNER, actor="owner_b")
        self.addCleanup(cancel_workspace_jobs, other.workspace_id)
        before = self._footprint(self.ctx, other)

        self.current = other
        for plan_id, product_id in ((plan["plan_id"], plan["product_id"]), ("atp_never_issued", "prd_never_issued")):
            self.assert_frozen(self._post_frozen(plan_id, product_id))

        # No denial audit either: the plan is never looked up.
        self.assertEqual(self._footprint(self.ctx, other), before)
        self.assertEqual(self._get(plan["plan_id"], expect=404)["code"], "test_plan_not_found")

    def test_a_plan_with_runs_stays_readable_and_replays_nothing(self):
        """Production's two ready plans each carry a sample Run."""
        plan = self._ready_plan()
        sample = self._run(plan["plan_id"], "sample")
        self._drain()
        before = self._footprint(self.ctx)

        self.assert_frozen(self._post_frozen(plan["plan_id"], plan["product_id"]))

        self.assertEqual(self._footprint(self.ctx), before)
        detail = self._get(plan["plan_id"])
        self.assertEqual([(run["stage"], run["run_id"]) for run in detail["runs"]], [("sample", sample["run_id"])])
        listed = self.client.get("/api/v1/test-plans", params={"run_id": sample["run_id"]})
        self.assertEqual([item["plan_id"] for item in listed.json()["items"]], [plan["plan_id"]])

    def test_create_does_not_replay_a_plan_the_domain_admitted(self):
        """#343: a key that admitted a plan before the freeze gets 410, not
        the plan; the plan stays readable."""
        product_id = self._product()
        admitted = self._create({"product_id": product_id}, key="ik-before-freeze")
        self._drain()
        before = self._footprint(self.ctx)

        self.assert_frozen([self.client.post(
            "/api/v1/test-plans", json={"product_id": product_id}, headers=self._headers("ik-before-freeze"),
        )])

        self.assertEqual(self._footprint(self.ctx), before)
        self.assertEqual(self._get(admitted["plan_id"])["status"], "ready")

    def test_create_still_validates_the_request_before_answering_410(self):
        """The request model and the Idempotency-Key header stay in the contract."""
        product_id = self._product()
        before = self._footprint(self.ctx)
        for body, headers in (
            ({"product_id": product_id}, {}),  # no Idempotency-Key
            ({}, self._headers()),
            ({"product_id": product_id, "scale": "huge"}, self._headers()),
            ({"product_id": product_id, "platform": "meta"}, self._headers()),  # extra=forbid
        ):
            response = self.client.post("/api/v1/test-plans", json=body, headers=headers)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["code"], "validation_error")
        self.assertEqual(self._footprint(self.ctx), before)
