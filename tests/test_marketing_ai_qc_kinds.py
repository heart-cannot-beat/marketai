"""Single-image and paired QC (#229): real HTTP, generation, durable jobs and costs; only the vision transport is faked."""
import asyncio
import copy
import unittest
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import JobRecord, WorkspaceRecord, new_id
from app.entitlements.cost_models import SpendReservationRecord
from app.marketing import ai_qc, workflow
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.ai_qc_worker import execute_ai_qc
from app.marketing.image_editing import execute_edit
from app.marketing.service import digest
from app.platform.jobs import enqueue_in_session
from app.platform.operations import create_operation
from tests.test_marketing_ai_qc import MarketingAIQCFixtures
from tests.test_marketing_board import SCREEN, executions_of, expression, plan


class TestMarketingAIQCKinds(MarketingAIQCFixtures):
    FLAGS = "?include_board=true&include_qc_kinds=true&include_image_edits=true"

    def round(self, row, flags=FLAGS):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}{flags}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def nodes(self, data):
        return {n["asset_version_id"]: n for n in executions_of(data["board"]) if n["asset_version_id"]}

    def calls(self, row):
        with tenant_session(self.context) as session:
            return {c.id: c for c in session.scalars(select(MarketingAIQCRecord).where(
                MarketingAIQCRecord.workspace_id == self.context.workspace_id, MarketingAIQCRecord.round_id == row["id"])).all()}

    def passing(self, provider, model, key, inputs, images):
        return self.report(inputs)

    def test_half_generated_round_checks_finished_images_now_and_pairs_once_group_b_exists(self):
        project, row = self.setup_round(count=3)
        self.assertEqual(len(row["assets"]), 3)
        snapshot = row["snapshot"]["plan"]
        group_of = {c: g["id"] for g in snapshot["groups"] for c in g["combination_ids"]}
        generated = {a["combination_id"] for a in row["assets"]}
        self.assertLess(len(generated), len(snapshot["combinations"]), "the round is still half generated")

        single = self.quote(row, "single")
        self.assertEqual((single["available"], single["kind"], single["quantity"], single["waiting"]), (True, "single", 3, []))
        row, _ = self.run_kind(row, "single")
        data = self.round(row)
        for asset in data["assets"]:
            self.assertEqual((asset["ai_qc"]["kind"], asset["ai_qc"]["state"], asset["ai_qc"]["current"]), ("single", "succeeded", True))
            self.assertIsNone(asset["paired_ai_qc"])
        ready, waiting = [], {}
        for vid, node in self.nodes(data).items():
            missing = [c["id"] for c in snapshot["combinations"]
                       if group_of[c["id"]] != group_of[node["combination_id"]] and c["id"] not in generated]
            self.assertEqual(node["awaiting_combination_ids"], missing)
            if missing:
                waiting[vid] = missing
                self.assertEqual((node["stage"], node["gap"], node["next_action"]), ("paired_qc_pending", "paired_qc", "none"))
            else:
                ready.append(vid)
                self.assertEqual((node["stage"], node["gap"], node["next_action"]), ("single_qc_done", "paired_qc", "paired_qc"))
        self.assertTrue(ready and waiting)

        paired = self.quote(row, "paired")
        self.assertEqual(sorted(paired["asset_version_ids"]), sorted(ready))
        self.assertEqual({w["asset_version_id"]: w["awaiting_combination_ids"] for w in paired["waiting"]}, waiting)
        row, early = self.run_kind(row, "paired")
        self.assertEqual(len(early), len(ready))

        self.run_drafts(self.pending_drafts)  # group B completes
        row = self.round(row)
        paired = self.quote(row, "paired")
        self.assertEqual(paired["waiting"], [])
        fresh = [a["asset_version_id"] for a in row["assets"] if a["asset_version_id"] not in ready]
        self.assertEqual(sorted(paired["asset_version_ids"]), sorted(fresh), "earlier paired results stay current")
        self.assertEqual(self.quote(row, "single")["quantity"], len(snapshot["combinations"]) - 3)
        row, _ = self.run_kind(row, "paired")
        nodes = self.nodes(self.round(row))
        self.assertEqual({n["stage"] for vid, n in nodes.items() if vid in waiting}, {"paired_qc_done"})

    def test_single_pass_but_paired_failure_blocks_approval_and_export(self):
        project, row = self.setup_round(paid=True)
        target = row["assets"][0]["asset_version_id"]

        async def vision(provider, model, key, inputs, images):
            report = self.report(inputs)
            if inputs["kind"] == "paired" and inputs["asset_version_id"] == target:
                next(f for f in report["findings"] if f["check_id"] == "belongs_to_intended_combination").update(status="fail",
                    visual_evidence="The image answers a buyer question in a Q&A card.",
                    rationale="It shows another expression, not the planned usage demonstration.",
                    suggested_fix="Show one real use step by step.")
            return report
        ids = self.run_both(row, AsyncMock(side_effect=vision))
        data = self.round(row)
        asset = next(a for a in data["assets"] if a["asset_version_id"] == target)
        self.assertNotEqual(asset["ai_qc"]["result"]["quality_suggestion"], "needs_changes", "the single-image check passed")
        self.assertEqual(asset["paired_ai_qc"]["result"]["quality_suggestion"], "needs_changes")
        node = self.nodes(data)[target]
        self.assertEqual((node["stage"], node["next_action"], node["paired_ai_qc"]["quality_suggestion"]), ("paired_qc_done", "review", "needs_changes"))

        bulk = {"ai_qc_ids": ids, "resolved_checks": {"product_fidelity": "pass"}, "note": "Product appearance checked against the unit."}
        refused = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/confirm", headers=self.headers(data["revision"]), json=bulk)
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "semantic_qc_unresolved"))
        own = {asset["ai_qc"]["id"], asset["paired_ai_qc"]["id"]}
        alone = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/confirm", headers=self.headers(data["revision"]),
            json={**bulk, "ai_qc_ids": [asset["ai_qc"]["id"]]})
        self.assertEqual((alone.status_code, alone.json()["code"]), (422, "ai_qc_kind_missing"), "a single-image pass never stands in for the comparison")
        row = self.command(data, "ai-qc/confirm", {**bulk, "ai_qc_ids": [i for i in ids if i not in own]})
        self.assertEqual({a["asset_version_id"] for a in row["assets"] if not a["ready"]}, {target})

        review = {"asset_version_id": target, "quality": "approved", "checks": {k: "pass" for k in asset["required_checks"]},
                  "note": "Reviewed the exact image.", "ai_qc_id": asset["ai_qc"]["id"], "compared_asset_version_ids": []}
        for body in (review, {**review, "paired_ai_qc_id": asset["paired_ai_qc"]["id"],
                              "compared_asset_version_ids": asset["paired_ai_qc"]["compared_asset_version_ids"]}):
            denied = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/reviews", headers=self.headers(row["revision"]), json=body)
            self.assertEqual((denied.status_code, denied.json()["code"]), (422, "qc_override_note_required"))
        everything = [a["asset_version_id"] for a in row["assets"]]
        blocked = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/handoff", headers=self.headers(row["revision"]), json={"asset_version_ids": everything})
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (422, "qc_required"))
        without = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/handoff", headers=self.headers(row["revision"]),
            json={"asset_version_ids": [v for v in everything if v != target]})
        self.assertEqual((without.status_code, without.json()["code"]), (422, "comparison_coverage_missing"))

        # Only an explicit human override with its basis can approve against the comparison result.
        row = self.command(row, "reviews", {**review, "override_note": "The Q&A card is the planned demonstration step; checked by hand."}, 201)
        quality = next(a for a in self.round(row)["assets"] if a["asset_version_id"] == target)["quality"]
        self.assertEqual(quality["qc_kinds"], {"single": "ai", "paired": "human"})

    def test_revision_reruns_both_kinds_and_keeps_every_old_record(self):
        project, row = self.setup_round()
        self.run_both(row)
        row = self.round(row)
        before = {c.id: (c.asset_version_id, c.kind, c.input_hash, copy.deepcopy(c.result_json)) for c in self.calls(row).values()}
        self.assertEqual(sorted(kind for _, kind, _, _ in before.values()), ["paired"] * 5 + ["single"] * 5)
        source = row["assets"][0]
        row = self.command(row, "reviews", {"asset_version_id": source["asset_version_id"], "quality": "needs_changes",
            "checks": {k: "fail" for k in source["required_checks"]}, "note": "Wrong scene; rotate and re-check."}, 201)
        row = self.command(row, "image-edits", {"asset_version_id": source["asset_version_id"], "rotation": 90}, 202)
        asyncio.run(execute_edit(self.context, row["image_edits"][0]["id"], self.blobs))
        data = self.round(row)
        child = next(a for a in data["assets"] if a["parent_version_id"] == source["asset_version_id"])
        self.assertEqual((child["ai_qc"], child["paired_ai_qc"], child["quality"]), (None, None, None))
        compared = [a for a in data["assets"] if source["asset_version_id"] in (a["paired_ai_qc"] or {}).get("compared_asset_version_ids", [])]
        self.assertTrue(compared and all(not a["paired_ai_qc"]["current"] for a in compared))
        self.assertEqual(self.quote(data, "single")["asset_version_ids"], [child["asset_version_id"]])
        self.assertEqual(sorted(self.quote(data, "paired")["asset_version_ids"]),
            sorted([child["asset_version_id"]] + [a["asset_version_id"] for a in compared]))

        self.run_both(data)
        data = self.round(row)
        child = next(a for a in data["assets"] if a["asset_version_id"] == child["asset_version_id"])
        self.assertEqual((child["ai_qc"]["kind"], child["paired_ai_qc"]["kind"]), ("single", "paired"))
        self.assertNotIn(source["asset_version_id"], child["paired_ai_qc"]["compared_asset_version_ids"])
        self.assertEqual(self.nodes(data)[child["asset_version_id"]]["stage"], "paired_qc_done")
        old = next(a for a in data["assets"] if a["asset_version_id"] == source["asset_version_id"])
        self.assertEqual((old["ai_qc"]["state"], old["quality"]["quality"]), ("succeeded", "needs_changes"))
        calls = self.calls(row)
        self.assertEqual(len(calls), 10 + 2 + len(compared))
        self.assertEqual({cid: (c.asset_version_id, c.kind, c.input_hash, c.result_json) for cid, c in calls.items() if cid in before}, before)

    def legacy_call(self, row, asset_id):
        """A call exactly as written before the split: kind NULL, policy v1, every check and comparison at once."""
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, row["id"])
            inputs = ai_qc.frozen_input(session, self.context, frozen, asset_id, kind=None)
            call_id = new_id("maqc")
            op = create_operation(session, command_type="marketing.ai_qc")
            job = enqueue_in_session(session, context=self.context, job_type="marketing.ai_qc", payload={"call_id": call_id},
                idempotency_key=call_id, resource_type="marketing_ai_qc", resource_id=call_id, safe_to_retry_after_crash=True, max_attempts=1)
            session.add(MarketingAIQCRecord(id=call_id, workspace_id=self.context.workspace_id, round_id=frozen.id, asset_version_id=asset_id,
                kind=None, operation_id=op.id, job_id=job.id, retry_of_id=None, sequence=1, state="queued", execution_mode="mock",
                provider="mock", model="marketing-qc-example-v1", maximum_cost="0", policy_version=ai_qc.POLICY,
                input_hash=digest(inputs), request_hash=digest([digest(inputs), None, "mock", "marketing-qc-example-v1", ai_qc.POLICY]),
                input_json=inputs))
            self.assertNotIn("kind", inputs)
            self.assertIn("comparison", [i["role"] for i in inputs["images"]])
        return call_id

    def test_pre_split_call_stays_readable_current_and_maps_to_the_new_stages(self):
        project, row = self.setup_round()
        target = row["assets"][0]["asset_version_id"]
        call_id = self.legacy_call(row, target)
        self.execute_checks(row)
        data = self.round(row)
        asset = next(a for a in data["assets"] if a["asset_version_id"] == target)
        self.assertEqual((asset["ai_qc"]["id"], asset["ai_qc"]["kind"], asset["ai_qc"]["current"]), (call_id, "combined", True))
        self.assertEqual(asset["paired_ai_qc"]["id"], call_id)
        self.assertNotIn("file_valid", [f["check_id"] for f in asset["ai_qc"]["result"]["findings"]])
        self.assertEqual((self.nodes(data)[target]["stage"], self.nodes(data)[target]["next_action"]), ("paired_qc_done", "review"))
        legacy = self.round(row, "?include_board=true")
        old = next(a for a in legacy["assets"] if a["asset_version_id"] == target)
        self.assertNotIn("kind", old["ai_qc"])
        self.assertNotIn("paired_ai_qc", old)
        self.assertEqual(self.nodes(legacy)[target]["stage"], "single_qc_done")
        for kind in ("single", "paired"):
            self.assertNotIn(target, self.quote(row, kind)["asset_version_ids"], "a current pre-split answer is not billed again")
        row = self.command(data, "reviews", {"asset_version_id": target, "quality": "approved", "checks": {k: "pass" for k in asset["required_checks"]},
            "note": "Reviewed the exact image.", "ai_qc_id": call_id, "compared_asset_version_ids": asset["ai_qc"]["compared_asset_version_ids"],
            "override_note": "The controlled Mock leaves every check open; each was checked by hand."}, 201)
        self.assertTrue(next(a for a in row["assets"] if a["asset_version_id"] == target)["ready"])
        quality = next(a for a in self.round(row)["assets"] if a["asset_version_id"] == target)["quality"]
        self.assertEqual(quality["qc_kinds"], {"single": "ai", "paired": "ai"})

    def test_each_kind_is_quoted_reserved_and_settled_on_its_own_and_uncertain_is_never_resent(self):
        project, row = self.setup_round(paid=True)
        single, paired = self.quote(row, "single"), self.quote(row, "paired")
        self.assertNotEqual(single["quote_hash"], paired["quote_hash"])
        legacy = self.quote(row)
        self.assertEqual(legacy["quote_hash"], single["quote_hash"], "clients from before the split run single checks")
        self.assertNotIn("kind", legacy)
        crossed = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/ai-qc", headers=self.headers(row["revision"]),
            json={"quote_hash": single["quote_hash"], "kind": "paired"})
        self.assertEqual((crossed.status_code, crossed.json()["code"]), (409, "qc_quote_changed"))
        row, single_ids = self.run_kind(row, "single", AsyncMock(side_effect=self.passing))
        row = self.round(row)
        timeout = AsyncMock(side_effect=TimeoutError("no provider response"))
        row, paired_ids = self.run_kind(row, "paired", timeout)
        for call_id in paired_ids:
            asyncio.run(execute_ai_qc(self.context, call_id, self.blobs, complete=timeout))
        self.assertEqual(timeout.await_count, 5)
        calls = self.calls(row)
        with tenant_session(self.context) as session:
            def reservation(call_id):
                return session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == calls[call_id].job_id))
            self.assertEqual({reservation(i).state for i in single_ids}, {"settled"})
            self.assertEqual({reservation(i).state for i in paired_ids}, {"uncertain"})
            self.assertTrue(all(reservation(i).outstanding_budget_amount > 0 for i in paired_ids))
            self.assertEqual({calls[i].policy_version for i in single_ids} | {calls[i].policy_version for i in paired_ids},
                {ai_qc.POLICIES["single"], ai_qc.POLICIES["paired"]})
        self.assertEqual(self.quote(row, "paired")["unavailable_reason"], "qc_in_progress")
        self.assertEqual(self.quote(row, "single")["unavailable_reason"], "nothing_to_check")

    def test_other_and_inactive_workspaces_cannot_quote_start_read_or_borrow_results(self):
        project, row = self.setup_round()
        row, single_ids = self.run_kind(row, "single")
        quote = self.quote(row, "paired")
        original = self.context
        self.context = self._make_context()
        path = f"/api/v1/marketing/rounds/{row['id']}"
        self.assertEqual(self.client.get(path + "/ai-qc/quote", params={"kind": "paired"}).status_code, 404)
        self.assertEqual(self.client.post(path + "/ai-qc", headers=self.headers(row["revision"]),
            json={"quote_hash": quote["quote_hash"], "kind": "paired"}).status_code, 404)
        self.assertEqual(self.client.get(path + self.FLAGS).status_code, 404)
        _, foreign = self.setup_round()
        asset = foreign["assets"][0]
        borrowed = self.client.post(f"/api/v1/marketing/rounds/{foreign['id']}/reviews", headers=self.headers(foreign["revision"]), json={
            "asset_version_id": asset["asset_version_id"], "quality": "approved", "checks": {k: "pass" for k in asset["required_checks"]},
            "note": "Borrowed result", "ai_qc_id": None, "paired_ai_qc_id": single_ids[0], "override_note": "x"})
        self.assertEqual((borrowed.status_code, borrowed.json()["code"]), (404, "ai_qc_not_found"))
        self.context = original
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "suspended"
        denied = self.client.post(path + "/ai-qc", headers=self.headers(row["revision"]), json={"quote_hash": quote["quote_hash"], "kind": "paired"})
        self.assertEqual(denied.status_code, 403, denied.text)
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "active"
        self.assertEqual({c.kind for c in self.calls(row).values()}, {"single"})
        with tenant_session(self.context) as session:
            self.assertFalse(session.scalars(select(JobRecord).where(JobRecord.workspace_id == self.context.workspace_id,
                JobRecord.type == "marketing.ai_qc", JobRecord.state == "queued")).all())


class TestRequiredChecks(unittest.TestCase):
    def required(self, frozen, cid):
        combo = {**next(c for c in frozen["combinations"] if c["id"] == cid), "checks": []}
        return workflow.required_checks(combo, frozen)

    def test_paired_checks_follow_the_plan_comparison_never_a_missing_image(self):
        between = self.required(SCREEN, "c1")
        self.assertIn("belongs_to_intended_combination", between)
        self.assertNotIn("expressions_distinct", between, "group 2 holds no other expression of angle a1")
        within = plan([("a1", [expression("x1"), expression("x2", mechanism="qa_objection")])],
            [("c1", "a1", "x1"), ("c2", "a1", "x2")], [("g1", ["c1"]), ("g2", ["c2"])], stage="match_expressions")
        self.assertIn("expressions_distinct", self.required(within, "c1"))
        legacy = plan([("a1", [expression("x1", mechanism=None), expression("x2", mechanism=None)])],
            [("c1", "a1", "x1"), ("c2", "a1", "x2")], [("g1", ["c1"]), ("g2", ["c2"])], stage="match_expressions")
        self.assertFalse({"belongs_to_intended_combination", "expressions_distinct"} & set(self.required(legacy, "c1")),
            "expressions frozen without their three elements keep the checks they were reviewed against")
        checks = self.required(within, "c1")
        self.assertEqual(set(workflow.kind_checks(checks, "paired")),
            {"belongs_to_intended_combination", "expressions_distinct", "comparison_faithful"})
        self.assertEqual(set(workflow.kind_checks(checks, "single")) | set(workflow.kind_checks(checks, "paired")), set(checks))
        self.assertEqual(workflow.kind_checks(checks, None), checks)


if __name__ == "__main__":
    unittest.main()
