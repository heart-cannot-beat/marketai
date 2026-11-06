"""Several executions of one expression (#225): planned per combination, numbered stably, exported per combination."""
import asyncio
import itertools
import json
import random
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock

from pydantic import ValidationError
from sqlalchemy import select

from app import config
from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import JobRecord, WorkspaceRecord, new_id
from app.generation.models import GenerationDraftRecord, RunItemRecord
from app.generation.paid_execution import execute_run_item
from app.marketing import executions, plan_review
from app.marketing.contracts import CreativeCombination, FrozenTestPlan, TestPlan as MarketingTestPlan
from app.marketing.evaluation import combination_key
from app.marketing.models import MarketingProjectRecord
from app.marketing.planner import example_result, system_prompt
from app.providers.adapter import ProviderCallResult
from tests.support.library_fixtures import png_bytes
from tests.test_marketing_board import SCREEN, board, by_version, execution, executions_of, expression, plan
from tests.test_marketing_workflow import MarketingWorkflowFixtures


def planned(base, count=2, **overrides):
    result = deepcopy(base)
    result["executions_per_combination"] = count
    for combo in result["combinations"]:
        if combo["id"] in overrides:
            combo["executions"] = overrides[combo["id"]]
    return result


def slots(result):
    return {c["combination_id"]: [(x["asset_version_id"], x["execution_index"], x["execution_version"]) for x in c["executions"]]
            for a in result["tree"]["angles"] for e in a["expressions"] for c in e["combinations"]}


class TestPlannedExecutions(unittest.TestCase):
    def test_each_expression_row_shows_its_planned_executions(self):
        result = board(planned(SCREEN), [execution("v1", "c1", 1)], generation_open=True)
        self.assertEqual(slots(result)["c1"], [("v1", 1, 1), (None, 2, 1)])
        self.assertEqual(slots(result)["c5"], [(None, 1, 1), (None, 2, 1)])
        combos = [c for a in result["tree"]["angles"] for e in a["expressions"] for c in e["combinations"]]
        self.assertEqual({c["planned_executions"] for c in combos}, {2})
        self.assertEqual({x["next_action"] for x in executions_of(result) if x["asset_version_id"] is None}, {"generate"})

    def test_numbers_follow_generation_order_and_never_shift(self):
        # The second image has the lexically smaller ID: generation order, not ID order, decides ① and ②.
        first, second = execution("vz", "c1", 1), execution("va", "c1", 2)
        numbered = slots(board(planned(SCREEN), [first, second]))["c1"]
        self.assertEqual(numbered, [("vz", 1, 1), ("va", 2, 1)])
        later = [execution("vm", "c1", 3), execution("v2", "c2", 4), second, first]
        random.Random(3).shuffle(later)
        self.assertEqual(slots(board(planned(SCREEN), later))["c1"][:2], numbered)

    def test_revisions_inherit_the_execution_number_with_the_next_version(self):
        assets = [execution("v1", "c1", 1), execution("v2", "c1", 2), execution("r1", "c1", 3, parent="v1"),
                  execution("r2", "c1", 4, parent="r1"), execution("r3", "c1", 5, parent="v1"),
                  execution("copy", "c1", 6, sha="sha-r1")]
        nodes = by_version(board(planned(SCREEN), assets))
        self.assertEqual({k: (n["execution_index"], n["execution_version"]) for k, n in nodes.items() if k},
            {"v1": (1, 1), "v2": (2, 1), "r1": (1, 2), "r2": (1, 3), "r3": (1, 4), "copy": (1, 2)})
        self.assertEqual(nodes["copy"]["duplicate_of"], "r1")
        self.assertNotIn(None, [vid for vid, _, _ in slots(board(planned(SCREEN), assets))["c1"]],
                         "revisions and copies fill no new slot, yet two distinct executions satisfy the plan")

    def test_legacy_plan_without_the_field_shows_execution_one(self):
        self.assertNotIn("executions_per_combination", SCREEN)
        result = board(SCREEN, [execution(f"v{n}", f"c{n}", n) for n in range(1, 6)])
        self.assertEqual(set(map(tuple, slots(result).values())), {((f"v{n}", 1, 1),) for n in range(1, 6)})
        combos = [c for a in result["tree"]["angles"] for e in a["expressions"] for c in e["combinations"]]
        self.assertEqual({c["planned_executions"] for c in combos}, {1})

    def test_a_combination_can_override_the_plan(self):
        result = board(planned(SCREEN, 1, c2=3), [])
        self.assertEqual([len(v) for v in slots(result).values()], [1, 3, 1, 1, 1])

    def test_unfilled_slots_take_no_action_once_delivery_is_frozen(self):
        ready = [execution(f"v{n}", f"c{n}", n, quality="approved", ready=True) for n in range(1, 6)]
        handoff = {"state": "queued", "entries": {f"v{n}": f"e{n}" for n in range(1, 6)}}
        result = board(planned(SCREEN), ready, handoff=handoff, round_state="handoff")
        pending = [x for x in executions_of(result) if x["asset_version_id"] is None]
        self.assertEqual(len(pending), 5)
        self.assertEqual({(x["stage"], x["gap"], x["next_action"]) for x in pending}, {("pending_generation", "none", "none")})

    def test_retest_plans_new_executions_beside_the_original(self):
        retest = planned(plan([("a1", [expression("x1")])], [("c1", "a1", "x1")], [("g1", ["c1"])],
            stage="confirm_combination", kind="business_retest"))
        result = board(retest, [execution("orig", "c1", 1, reused=True), execution("new", "c1", 2)], generation_open=True)
        self.assertEqual(slots(result)["c1"], [("orig", 1, 1), ("new", 2, 1), (None, 3, 1)])


class TestExecutionContracts(unittest.TestCase):
    def plan(self, **changes):
        return {**example_result("plan", {"understanding": {"facts": [{"id": "f1"}], "audience": "Commuters"},
                                           "strategy_language": "en-US"}), **changes}

    def test_new_plans_default_to_two_executions_and_legacy_plans_mean_one(self):
        drafted = MarketingTestPlan.model_validate(self.plan())
        self.assertEqual(drafted.executions_per_combination, 2)
        omitted = self.plan()
        omitted.pop("executions_per_combination")
        self.assertEqual(MarketingTestPlan.model_validate(omitted).executions_per_combination, 2)
        self.assertEqual(FrozenTestPlan.model_validate(omitted).executions_per_combination, 1)
        for count in (0, 5):
            with self.assertRaises(ValidationError):
                MarketingTestPlan.model_validate(self.plan(executions_per_combination=count))
        combo = self.plan()["combinations"][0]
        self.assertIsNone(CreativeCombination.model_validate(combo).executions)
        self.assertEqual(CreativeCombination.model_validate({**combo, "executions": 4}).executions, 4)
        with self.assertRaises(ValidationError):
            CreativeCombination.model_validate({**combo, "executions": 5})

    def test_planner_is_told_the_default_and_the_budget_limitation(self):
        prompt = system_prompt("plan", {})
        self.assertIn("executions_per_combination", prompt)
        self.assertIn("默认2", prompt)
        self.assertIn("预算紧张时可设为1，并在limitations", prompt)
        self.assertIn('"executions_per_combination"', prompt, "the schema the planner fills carries the field")

    def test_count_is_not_creative_meaning(self):
        snapshot = {"plan": self.plan(), "understanding": {"facts": []}}
        legacy = deepcopy(snapshot)
        for combo in legacy["plan"]["combinations"]:
            combo.pop("executions", None)
        entry = {"combination_id": snapshot["plan"]["combinations"][0]["id"]}
        overridden = deepcopy(snapshot)
        overridden["plan"]["combinations"][0]["executions"] = 3
        self.assertEqual(combination_key(snapshot, entry), combination_key(legacy, entry))
        self.assertEqual(combination_key(overridden, entry), combination_key(legacy, entry))

    def test_an_omitting_client_keeps_the_stored_counts(self):
        stored = MarketingTestPlan.model_validate(self.plan(executions_per_combination=3)).model_dump(mode="json")
        stored["combinations"][1]["executions"] = 1
        sent = {k: v for k, v in stored.items() if k != "executions_per_combination"}
        sent["combinations"] = [{k: v for k, v in c.items() if k != "executions"} for c in stored["combinations"]]
        kept = executions.keep_counts(stored, MarketingTestPlan.model_validate(sent))
        self.assertEqual((kept.executions_per_combination, [c.executions for c in kept.combinations][:2]), (3, [None, 1]))
        legacy = executions.legacy_projection(stored)
        self.assertEqual(executions.keep_counts(legacy, MarketingTestPlan.model_validate(sent)).executions_per_combination, 1)
        chosen = executions.keep_counts(stored, MarketingTestPlan.model_validate({**sent, "executions_per_combination": 2}))
        self.assertEqual(chosen.executions_per_combination, 2)
        self.assertEqual(executions.keep_counts(None, MarketingTestPlan.model_validate(sent)).executions_per_combination, 2)


class TestExecutionsHttp(MarketingWorkflowFixtures):
    EXECUTION_PLAN = "?include_languages=true&include_plan_review=true&include_execution_plan=true"

    def prepare(self, project, **body):
        return self.client.post(f"/api/v1/marketing/projects/{project['id']}/generation-drafts{self.EXECUTION_PLAN}",
            headers=self.headers(project["revision"]), json={"provider": "mock", "model": "mock-v1", **body})

    def generate_distinct(self, project):
        """Each provider call returns a different file, so every planned execution is a real, distinct image."""
        colours = itertools.count(1)
        adapter = AsyncMock()
        adapter.execute.side_effect = lambda *args, **kwargs: ProviderCallResult(
            png_bytes(colour=(next(colours) * 20 % 256, 40, 80)), "external-request", False)
        self.blobs = LocalFileBlobStore(root=config.DATA_DIR / new_id("blobs"), public_base_url="http://localhost:9000", signing_secret=b"test-only")
        prepared = self.prepare(project)
        self.assertEqual(prepared.status_code, 201, prepared.text)
        for draft_id in prepared.json()["generation_draft_ids"]:
            draft = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()
            preview = self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(draft["revision"])).json()
            run = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
                json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]})
            self.assertEqual(run.status_code, 202, run.text)
            with tenant_session(self.context) as session:
                items = [(item.id, session.scalar(select(JobRecord.id).where(JobRecord.workspace_id == self.context.workspace_id, JobRecord.resource_id == item.id)))
                    for item in session.scalars(select(RunItemRecord).where(RunItemRecord.generation_run_id == run.json()["generation_run_id"],
                        RunItemRecord.workspace_id == self.context.workspace_id).order_by(RunItemRecord.variant_index)).all()]
            for item_id, job_id in items:
                asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))

    def confirm(self, project, plan=None):
        response = self.edit(project, "confirm_plan", plan=plan or project["plan"])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def round(self, row, query=EXECUTION_PLAN + "&include_board=true"):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}{query}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_default_draft_prepares_each_combinations_planned_executions(self):
        project = self.drafted()
        plan = project["plan"]
        self.assertEqual(plan["executions_per_combination"], 2)
        plan["combinations"].append({**plan["combinations"][0], "id": "c1_3", "executions": 1})
        plan["groups"][0]["combination_ids"].append("c1_3")
        project = self.confirm(project, plan)
        self.assertEqual(len(project["plan"]["combinations"]), 6)
        prepared = self.prepare(project)
        self.assertEqual(prepared.status_code, 201, prepared.text)
        drafts = prepared.json()["generation_draft_ids"]
        self.assertEqual(len(drafts), 6)
        variants = [self.client.get(f"/api/v1/generation/drafts/{d}").json()["format_config"]["variants"] for d in drafts]
        self.assertEqual(variants, [2, 2, 1, 2, 2, 2])
        draft = self.client.get(f"/api/v1/generation/drafts/{drafts[0]}").json()
        preview = self.client.post(f"/api/v1/generation/drafts/{drafts[0]}/previews", headers=self.headers(draft["revision"]))
        self.assertEqual(preview.status_code, 201, preview.text)
        self.assertEqual(preview.json()["quantity"], 2, "the cost preview covers both executions")

        other = self.confirm(self.drafted())
        response = self.prepare(other, variants=1)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual({self.client.get(f"/api/v1/generation/drafts/{d}").json()["format_config"]["variants"]
                          for d in response.json()["generation_draft_ids"]}, {1}, "an explicit override (pre-#225 clients) still applies")

    def test_one_approved_execution_per_combination_exports_with_numbered_ad_names(self):
        project = self.confirm(self.drafted())
        self.generate_distinct(project)
        row = self.start_round(project)
        data = self.round(row)
        self.assertEqual(len(data["assets"]), 10)
        for combo in (c for a in data["board"]["tree"]["angles"] for e in a["expressions"] for c in e["combinations"]):
            self.assertEqual(combo["planned_executions"], 2)
            self.assertEqual([(x["execution_index"], x["execution_version"], x["duplicate_of"]) for x in combo["executions"]],
                             [(1, 1, None), (2, 1, None)])
            self.assertTrue(all(x["asset_version_id"] for x in combo["executions"]))
        self.assertEqual(self.round(row)["board"], data["board"], "re-reading never renumbers")

        numbers = {x["asset_version_id"]: x["execution_index"] for x in executions_of(data["board"])}
        first = [a for a in data["assets"] if numbers[a["asset_version_id"]] == 1]
        both = first + [a for a in data["assets"] if a["combination_id"] == "c1_1" and numbers[a["asset_version_id"]] == 2]
        for asset in both:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Checked product, proposition, copy and comparison"}, 201)
        partial = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/handoff", headers=self.headers(row["revision"]),
            json={"asset_version_ids": [a["asset_version_id"] for a in first[:-1]]})
        self.assertEqual(partial.json()["code"], "comparison_coverage_missing", "coverage is still checked per combination")
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in both]}, 202)
        names = {e["asset_version_id"]: e["ad_name"] for e in row["handoff"]["entries"]}
        self.assertEqual(len(names), 6)
        for vid, name in names.items():
            self.assertTrue(name.endswith(f" #{numbers[vid]}"), name)
        self.assertEqual(sorted(name[-2:] for name in names.values()), ["#1"] * 5 + ["#2"])
        board = self.round(row)["board"]
        unexported = [x for x in executions_of(board) if x["asset_version_id"] not in names]
        self.assertEqual({(x["gap"], x["next_action"]) for x in unexported}, {("none", "none")})

    def test_legacy_round_without_the_field_shows_execution_one_and_still_opens(self):
        project = self.confirmed()
        with tenant_session(self.context) as session:
            record = session.get(MarketingProjectRecord, project["id"])
            record.plan_json = executions.legacy_projection(record.plan_json)  # stored before #225
        project = self.read(project)
        self.assertNotIn("executions_per_combination", project["plan"])
        self.generate(project, variants=None)
        row = self.start_round(project)
        data = self.round(row)
        self.assertEqual(len(data["assets"]), 5, "the frozen intent stays byte-identical, so every image belongs to the round")
        self.assertNotIn("executions_per_combination", data["snapshot"]["plan"])
        for combo in (c for a in data["board"]["tree"]["angles"] for e in a["expressions"] for c in e["combinations"]):
            self.assertEqual(combo["planned_executions"], 1)
            self.assertEqual([(x["execution_index"], x["execution_version"]) for x in combo["executions"]], [(1, 1)])
        with tenant_session(self.context) as session:
            drafts = session.scalars(select(GenerationDraftRecord).where(GenerationDraftRecord.workspace_id == self.context.workspace_id)).all()
            self.assertEqual({d.format_json["variants"] for d in drafts}, {1})
            self.assertTrue(all("executions" not in d.selectors_json["marketing_intent"]["combination"] for d in drafts))

        saved = self.edit(project, "save_plan", plan=project["plan"])
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["plan"]["executions_per_combination"], 1, "re-saving keeps the legacy meaning")
        # A client predating both the brand baseline and #225 sends neither; restoring style must not reset the count.
        older = executions.legacy_projection(plan_review.legacy_style_projection(saved.json()["plan"]))
        resaved = self.edit(saved.json(), "save_plan", plan=older)
        self.assertEqual(resaved.status_code, 200, resaved.text)
        self.assertEqual(resaved.json()["plan"]["executions_per_combination"], 1)
        self.assertEqual(resaved.json()["plan"]["style_variation"], saved.json()["plan"]["style_variation"])

    def test_new_fields_are_opt_in_for_strict_clients(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        legacy = self.client.get(f"/api/v1/marketing/projects/{project['id']}?include_languages=true&include_plan_review=true").json()
        self.assertNotIn("executions_per_combination", legacy["plan"])
        self.assertFalse(any("executions" in c for c in legacy["plan"]["combinations"]))
        data = self.round(row, "?include_board=true")
        self.assertNotIn("executions_per_combination", data["snapshot"]["plan"])
        nodes = executions_of(data["board"])
        self.assertTrue(nodes and all("execution_version" not in x for x in nodes))
        self.assertFalse(any("planned_executions" in c for a in data["board"]["tree"]["angles"] for e in a["expressions"] for c in e["combinations"]))
        workflow = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow{self.EXECUTION_PLAN}&include_board=true").json()
        self.assertEqual(workflow["current"]["snapshot"]["plan"]["executions_per_combination"], 1)
        self.assertTrue(all(x["execution_version"] == 1 for x in executions_of(workflow["current"]["board"])))

    def test_execution_commands_do_not_cross_tenants_or_survive_suspension(self):
        project = self.confirm(self.drafted())
        original = self.context
        self.context = self._make_context()
        self.assertEqual(self.prepare(project).status_code, 404)
        self.context = original
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "suspended"
        denied = self.prepare(project)
        self.assertEqual((denied.status_code, denied.json()["code"]), (403, "workspace_inactive"))
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "active"
        with tenant_session(self.context) as session:
            self.assertEqual(session.scalars(select(GenerationDraftRecord).where(
                GenerationDraftRecord.workspace_id == self.context.workspace_id)).all(), [])

        self.generate_distinct(project)
        row = self.start_round(project)
        assets = self.round(row)["assets"]
        self.context = self._make_context()
        self.assertEqual(self.client.get(f"/api/v1/marketing/rounds/{row['id']}{self.EXECUTION_PLAN}&include_board=true").status_code, 404)
        foreign = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/handoff", headers=self.headers(row["revision"]),
            json={"asset_version_ids": [a["asset_version_id"] for a in assets]})
        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.context = original
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "suspended"
        denied = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/handoff", headers=self.headers(row["revision"]),
            json={"asset_version_ids": [a["asset_version_id"] for a in assets]})
        self.assertEqual(denied.status_code, 403, denied.text)
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "active"
        self.assertIsNone(self.round(row)["handoff"])


if __name__ == "__main__":
    unittest.main()
