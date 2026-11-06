"""#292: an English image brief written for an earlier version of its expression blocks the plan.

The brief is what the image model and the Meta ad name read, so editing an expression's name,
way of telling or visual mechanism without rewriting the brief would generate and name the old
expression. Plans stored before this check have no record and are never blocked for it.
"""
import unittest
from copy import deepcopy

from app.adapters.persistence.database import tenant_session
from app.marketing import plan_review
from app.marketing.expression_catalog import english_name
from app.marketing.models import MarketingProjectRecord
from app.marketing.service import digest, image_brief
from app.marketing.contracts import CreativeCombination
from tests.test_marketing_export_language import ExportLanguageFixtures

NAME = "High power and certified safety"
SCENE = "Close-up of the charger's three ports and the 65W rating, each labelled with one documented fact."


class StaleBriefFixtures(ExportLanguageFixtures):
    def edit_expression(self, plan, **fields):
        """The first expression of the first angle, and the combination that executes it."""
        plan = deepcopy(plan)
        expression = plan["angles"][0]["expressions"][0]
        expression.update(fields)
        combo = next(c for c in plan["combinations"] if c["expression_id"] == expression["id"])
        return plan, expression, combo

    def rewrite_brief(self, plan, combination_id, *, expression=NAME, scene=SCENE):
        plan = deepcopy(plan)
        combo = next(c for c in plan["combinations"] if c["id"] == combination_id)
        if expression is not None:
            combo["image_brief"]["expression"] = expression
        if scene is not None:
            combo["visual_brief"] = scene
        return plan

    def stale(self, project):
        return [r for r in project["plan"]["plan_review"] if r["kind"] == "image_brief_stale"]

    def stored(self, project):
        with tenant_session(self.context) as session:
            return deepcopy(session.get(MarketingProjectRecord, project["id"]).plan_json)

    def save(self, project, plan, command="save_plan", status=200):
        response = self.edit(project, command, plan=plan)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()


class TestStaleImageBrief(StaleBriefFixtures):
    def test_editing_an_expression_blocks_until_the_brief_is_rewritten_then_prompt_and_ad_name_follow(self):
        project = self.drafted()
        plan, expression, combo = self.edit_expression(project["plan"], name=NAME, visual_mechanism="feature_closeup",
            how_to_tell="Zoom into the ports and the rating label so the power claim is seen, not asserted.")
        index = next(i for i, c in enumerate(plan["combinations"]) if c["id"] == combo["id"])
        self.assertEqual(combo["image_brief"]["expression"], "Usage demonstration")

        saved = self.save(project, plan)
        [item] = self.stale(saved)
        self.assertEqual((item["severity"], item["source"], item["resolved"]), ("blocking", "system", False))
        self.assertEqual(item["target_ids"], [combo["id"], expression["id"]])
        self.assertIn(f'"{saved["plan"]["angles"][0]["proposition"]} / {NAME}"', item["finding"])
        self.assertIn('still describes "Usage demonstration"', item["finding"])
        self.assertIn("Rewrite this combination's English image brief", item["suggestion"])
        self.assertNotIn(plan_review.BRIEF_BASIS, saved["plan"])

        blocked = self.edit(saved, "confirm_plan", plan={**saved["plan"], "executions_per_combination": 1})
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()["code"], "image_brief_stale")
        self.assertEqual([e["field"] for e in blocked.json()["field_errors"]], [f"body.plan.combinations.{index}.image_brief.expression"])
        self.assertEqual(self.read(saved)["revision"], saved["revision"])

        # Rewriting the brief in the existing editor clears the block; no paid call is involved.
        fixed = self.rewrite_brief(saved["plan"], combo["id"])
        confirmed = self.save(saved, {**fixed, "executions_per_combination": 1}, "confirm_plan")
        self.assertEqual(self.stale(confirmed), [])
        self.assertEqual(confirmed["state"], "plan_confirmed")

        self.generate(confirmed)
        drafts = [self.client.get(f"/api/v1/generation/drafts/{d}").json() for d in self.read(confirmed)["generation_draft_ids"]]
        prompt = next(d["selectors"]["prompt"] for d in drafts if d["selectors"]["marketing_intent"]["combination"]["id"] == combo["id"])
        self.assertIn(f"\nExpression: {NAME}\nVisual mechanism: Feature close-up\nScene: {SCENE}\n", prompt)
        self.assertNotIn("Usage demonstration", prompt)
        self.assertNotIn("usage demonstration", prompt)

        row = self.start_round(confirmed)
        for asset in row["assets"]:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Checked product, proposition, copy and comparison"}, 201)
        row = self.export(row)
        [entry] = [e for e in row["handoff"]["entries"] if e["combination_id"] == combo["id"]]
        self.assertIn(f" / {NAME} #1", entry["ad_name"])
        self.assertEqual(entry["display_name"], f"{entry['proposition']} / {NAME} · Execution 1")

    def test_every_combination_prompt_names_its_catalog_mechanism(self):
        project = self.drafted()
        mechanisms = {e["id"]: e["visual_mechanism"] for a in project["plan"]["angles"] for e in a["expressions"]}
        confirmed = self.save(project, {**project["plan"], "executions_per_combination": 1}, "confirm_plan")
        self.generate(confirmed)
        for draft_id in self.read(confirmed)["generation_draft_ids"]:
            selectors = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()["selectors"]
            mechanism = mechanisms[selectors["marketing_intent"]["combination"]["expression_id"]]
            self.assertIn(f"\nVisual mechanism: {english_name(mechanism)}\nScene: ", selectors["prompt"])

    def test_either_part_of_the_brief_counts_as_a_rewrite_but_other_edits_do_not(self):
        project = self.drafted()
        plan, _, combo = self.edit_expression(project["plan"], name=NAME)
        saved = self.save(project, plan)
        self.assertEqual(len(self.stale(saved)), 1)
        headline_only = deepcopy(saved["plan"])
        next(c for c in headline_only["combinations"] if c["id"] == combo["id"])["headline"] = "Power you can check"
        self.assertEqual(len(self.stale(self.save(saved, headline_only))), 1)
        saved = self.read(saved)
        self.assertEqual(self.stale(self.save(saved, self.rewrite_brief(saved["plan"], combo["id"], scene=None))), [])

        project = self.drafted()
        plan, _, combo = self.edit_expression(project["plan"], visual_mechanism="feature_closeup")
        saved = self.save(project, plan)
        self.assertEqual(len(self.stale(saved)), 1)
        self.assertEqual(self.stale(self.save(saved, self.rewrite_brief(saved["plan"], combo["id"], expression=None))), [])

    def test_restoring_the_expression_clears_the_block_and_a_later_edit_blocks_again(self):
        project = self.drafted()
        original = project["plan"]
        plan, _, combo = self.edit_expression(original, how_to_tell="Show it charging three devices at a gate.")
        saved = self.save(project, plan)
        self.assertEqual(len(self.stale(saved)), 1)
        restored, _, _ = self.edit_expression(saved["plan"], how_to_tell=original["angles"][0]["expressions"][0]["how_to_tell"])
        saved = self.save(saved, restored)
        self.assertEqual(self.stale(saved), [])

        rewritten = self.save(saved, self.rewrite_brief(saved["plan"], combo["id"]))
        plan, _, _ = self.edit_expression(rewritten["plan"], name="Certified fast charging")
        self.assertEqual([r["target_ids"][0] for r in self.stale(self.save(rewritten, plan))], [combo["id"]])

    def test_a_shared_expression_flags_each_combination_that_executes_it(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        shared = deepcopy(plan["angles"][0]["expressions"][0])
        plan["angles"][1]["expressions"].append({**shared, "fit_reason": "The same telling also answers the concern."})
        plan["combinations"].append({**deepcopy(next(c for c in plan["combinations"] if c["expression_id"] == shared["id"])),
                                     "id": "c_shared", "angle_id": plan["angles"][1]["id"]})
        plan["groups"][1]["combination_ids"].append("c_shared")
        project = self.save(project, plan)
        self.assertEqual(self.stale(project), [])
        plan = deepcopy(project["plan"])
        for angle in plan["angles"]:
            for expression in angle["expressions"]:
                if expression["id"] == shared["id"]:
                    expression["name"] = NAME
        flagged = {r["target_ids"][0] for r in self.stale(self.save(project, plan))}
        self.assertEqual(flagged, {c["id"] for c in plan["combinations"] if c["expression_id"] == shared["id"]})
        self.assertEqual(len(flagged), 2)


class TestServerSideRecord(StaleBriefFixtures):
    def test_the_record_is_server_bookkeeping_that_clients_cannot_forge_or_read(self):
        project = self.drafted()
        stored = self.stored(project)
        basis = stored[plan_review.BRIEF_BASIS]
        self.assertEqual(set(basis), {c["id"] for c in stored["combinations"]})
        self.assertNotIn(plan_review.BRIEF_BASIS, project["plan"])

        plan, expression, combo = self.edit_expression(project["plan"], name=NAME)
        # A client cannot clear the block by dropping the system item or by sending a record of its own.
        forged = self.edit(project, "save_plan", plan={**plan, plan_review.BRIEF_BASIS: {combo["id"]: "0" * 64}})
        self.assertEqual(forged.status_code, 422, forged.text)
        saved = self.save(project, {**plan, "plan_review": [r for r in plan["plan_review"] if r["source"] != "system"]})
        self.assertEqual(len(self.stale(saved)), 1)
        self.assertEqual(self.stored(saved)[plan_review.BRIEF_BASIS][combo["id"]], basis[combo["id"]])

        rewritten = self.save(saved, self.rewrite_brief(saved["plan"], combo["id"]))
        self.assertNotEqual(self.stored(rewritten)[plan_review.BRIEF_BASIS][combo["id"]], basis[combo["id"]])

    def test_a_round_freezes_the_plan_document_and_its_hash_matches_the_prepared_intent(self):
        project = self.drafted()
        confirmed = self.save(project, {**project["plan"], "executions_per_combination": 1}, "confirm_plan")
        self.generate(confirmed)
        row = self.start_round(confirmed)
        snapshot = self.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_languages=true&include_plan_review=true"
                                   "&include_brand_style=true&include_execution_plan=true").json()["data"]["snapshot"]
        self.assertNotIn(plan_review.BRIEF_BASIS, snapshot["plan"])
        self.assertEqual(snapshot["plan_hash"], digest(snapshot["plan"]))
        self.assertEqual(snapshot["plan_hash"], digest(plan_review.plan_document(self.stored(confirmed))))
        draft = self.client.get(f"/api/v1/generation/drafts/{self.read(confirmed)['generation_draft_ids'][0]}").json()
        self.assertEqual(draft["selectors"]["marketing_intent"]["plan_hash"], snapshot["plan_hash"])
        # Every asset matched its frozen combination and plan hash, so the round lists all of them.
        self.assertEqual(len(row["assets"]), len(confirmed["plan"]["combinations"]))

    def test_preparing_creatives_refuses_a_stale_brief_on_a_confirmed_plan(self):
        project = self.drafted()
        confirmed = self.save(project, {**project["plan"], "executions_per_combination": 1}, "confirm_plan")
        # Confirmation refuses a stale brief, so only a stored plan can carry one; the generation seam checks again.
        with tenant_session(self.context) as session:
            record = session.get(MarketingProjectRecord, confirmed["id"])
            first = record.plan_json["combinations"][0]["id"]
            record.plan_json = {**record.plan_json, plan_review.BRIEF_BASIS: {
                **record.plan_json[plan_review.BRIEF_BASIS], first: "0" * 64}}
        response = self.client.post(f"/api/v1/marketing/projects/{confirmed['id']}/generation-drafts",
            headers=self.headers(confirmed["revision"]), json={"provider": "mock", "model": "mock-v1", "variants": 1})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "image_brief_stale")
        self.assertEqual([e["field"] for e in response.json()["field_errors"]], ["body.plan.combinations.0.image_brief.expression"])
        self.assertEqual(self.read(confirmed)["generation_draft_ids"], [])

    def test_a_plan_stored_before_the_record_is_not_blocked_retroactively(self):
        project = self.drafted()
        with tenant_session(self.context) as session:
            record = session.get(MarketingProjectRecord, project["id"])
            record.plan_json = plan_review.plan_document(record.plan_json)
        plan, _, combo = self.edit_expression(project["plan"], name=NAME, visual_mechanism="feature_closeup")
        saved = self.save(project, plan)
        self.assertEqual(self.stale(saved), [])
        self.assertNotIn(plan_review.BRIEF_BASIS, self.stored(saved))
        confirmed = self.save(saved, {**saved["plan"], "executions_per_combination": 1}, "confirm_plan")
        self.assertEqual(confirmed["state"], "plan_confirmed")
        # Once the user writes a brief, that combination is checked from then on.
        rewritten = self.save(confirmed, self.rewrite_brief(confirmed["plan"], combo["id"]))
        self.assertEqual(set(self.stored(rewritten)[plan_review.BRIEF_BASIS]), {combo["id"]})
        plan, _, _ = self.edit_expression(rewritten["plan"], name="Certified fast charging")
        self.assertEqual(len(self.stale(self.save(rewritten, plan))), 1)


class TestChineseStrategyFinding(StaleBriefFixtures):
    STRATEGY_LANGUAGE = "zh-CN"
    SOURCE_TEXT = "Alpha 充电器\n65W，多口，面向差旅办公用户。"

    def test_the_finding_is_written_in_the_strategy_language(self):
        project = self.drafted()
        plan, expression, _ = self.edit_expression(project["plan"], name="高功率与认证保障", visual_mechanism="feature_closeup")
        [item] = self.stale(self.save(project, plan))
        self.assertIn(f"「{plan['angles'][0]['proposition']} / 高功率与认证保障」的英文画面要求", item["finding"])
        self.assertIn("仍按「Usage demonstration」描述画面", item["finding"])
        self.assertEqual(item["suggestion"], "按当前表达方式改写该组合的英文画面要求（表达方式（英文））和画面要求。")


class TestMechanismLine(unittest.TestCase):
    def intent(self, mechanism):
        expression = {"id": "e1", **({"visual_mechanism": mechanism} if mechanism else {})}
        return {"style_variation": "held", "ad_language": "en-US", "expression": expression}

    def combination(self):
        return CreativeCombination.model_validate({"id": "c1", "angle_id": "a1", "expression_id": "e1", "headline": "Explore",
            "body_copy": "Explore actual uses.", "visual_brief": "Show the product.", "checks": ["Accurate"],
            "image_brief": {"proposition": "Value", "expression": "Close-up", "hold_constant": ["Same offer"], "checks": ["Accurate"]}})

    def test_catalog_keys_use_the_english_name_and_custom_labels_only_when_english(self):
        cases = {"feature_closeup": "Feature close-up", "live_connection_demo": "Live hands-on demonstration",
                 "other:  Before and after cable test ": "Before and after cable test",
                 "other:充电前后对比": None, "retired_mechanism": None, None: None}
        for mechanism, name in cases.items():
            prompt = image_brief(self.combination(), self.intent(mechanism))
            line = f"Visual mechanism: {name}\n" if name else ""
            self.assertIn(f"\nExpression: Close-up\n{line}Scene: Show the product.\n", prompt, mechanism)
            self.assertEqual(english_name(mechanism), name, mechanism)
