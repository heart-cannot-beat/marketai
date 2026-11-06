"""Issue #166 -- the results loop end to end over HTTP.

Real routes, the real Worker for plan drafting and the Mock generation Run
items, then: results template -> CSV / manual / Meta-synced import preview
-> commit -> learnings snapshot -> product learnings and plan results ->
"按结论生成下一轮" seeds the drafter.

* Mapping: every generated image maps to its plan direction and factor
  values; external uploads are reported as unattributable.
* Import: validation reasons, idempotency (same file never imported twice),
  overlap refusal and same-range replacement, Mock partition isolation.
* Learnings: "有人 > 无人"-style sentences with interval and n; insufficient
  data says so; snapshots are immutable and reused for identical inputs.
* WinnersLosers: the latest production learnings and an explicit snapshot
  reach the drafter; the mock drafter skips losers but keeps one
  exploratory direction.
* Negative tenant and permission paths; audit events.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import select, update

from tests.test_test_plans_api import PlanFixtures, openai_completion
from tests.support.queue_hygiene import cancel_workspace_jobs

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AdDailyMetricRecord,
    AdTestPlanRecord,
    AssetRecord,
    AssetVersionRecord,
    LearningSnapshotRecord,
    MetaAccountRecord,
    MetaAdBindingRecord,
    MetaSyncRunRecord,
    ResultObservationRecord,
    utcnow,
)
from app.learnings import codes, parsing
from app.marketing.languages import CJK
from app.plans import drafting, llm_drafter
from app.providers.llm_mock import TEST_PLAN_DRAFT_EXAMPLE
from app.library.contracts import ROLE_OWNER
from app.meta.models import MetaConnectionRecord
from app.platform.tenancy import TenantContext

TODAY = utcnow().date()
START = (TODAY - timedelta(days=14)).isoformat()
END = (TODAY - timedelta(days=8)).isoformat()
HEADERS = ["素材编号", "方向", "画面组合", "开始日期", "结束日期", "展示次数", "链接点击量", "花费", "购买次数", "币种"]


def _ctr_for(combination: str) -> float:
    """真人使用 converts clicks better than 纯产品; nothing else matters."""
    return 0.021 if "真人使用" in combination else 0.012


class LearningsFixtures(PlanFixtures):
    def _generated(self, *, product_id: str | None = None) -> tuple[dict, str]:
        product_id = product_id or self._product()
        plan = self._ready_plan(product_id=product_id)
        self._run(plan["plan_id"], "sample")
        self._drain()
        self._run(plan["plan_id"], "full")
        self._drain()
        return plan, product_id

    def _template(self, product_id: str, *, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/products/{product_id}/results/template")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _csv(self, template: dict, *, impressions: int = 2400, start: str = START, end: str = END,
             conversions: int = 1) -> str:
        """Every image of the template: 真人使用 has the higher CTR, CPA is equal."""
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(HEADERS)
        for index, row in enumerate(template["rows"]):
            if not row["attributable"]:
                continue
            ctr = _ctr_for(row["combination"]) * (1 + 0.04 * ((index % 3) - 1))
            writer.writerow([
                row["asset_code"], row["direction"], row["combination"], start, end,
                impressions, int(impressions * ctr), "20.00", conversions, "USD",
            ])
        return out.getvalue()

    def _import(self, body: dict, *, key: str | None = None, expect: int = 201) -> dict:
        response = self.client.post("/api/v1/results/imports", json=body, headers=self._headers(key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _commit(self, import_id: str, *, expect: int = 200) -> dict:
        response = self.client.post(f"/api/v1/results/imports/{import_id}/commit", headers=self._headers())
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _learnings(self, product_id: str, *, expect: int = 200, **params) -> dict:
        response = self.client.get(f"/api/v1/products/{product_id}/learnings", params=params)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _observations(self) -> int:
        with tenant_session(self.ctx) as session:
            return len(session.scalars(
                select(ResultObservationRecord).where(ResultObservationRecord.workspace_id == self.ctx.workspace_id)
            ).all())

    def _make_byok(self, product_id: str) -> None:
        """Treat the product's images as BYOK output (a real-evidence partition)."""
        with tenant_session(self.ctx) as session:
            asset_ids = select(AssetRecord.id).where(AssetRecord.product_id == product_id)
            session.execute(
                update(AssetVersionRecord)
                .where(AssetVersionRecord.workspace_id == self.ctx.workspace_id, AssetVersionRecord.asset_id.in_(asset_ids))
                .values(execution_mode="byok")
            )

    def _value(self, round_: dict, factor_label: str, value_label: str) -> dict:
        factor = next(item for item in round_["factors"] if item["label"] == factor_label)
        return next(item for item in factor["values"] if item["label"] == value_label)


class TestTemplateAndMapping(LearningsFixtures):
    def test_template_lists_every_image_with_a_code_and_its_combination(self):
        plan, product_id = self._generated()
        self.current = replace(self.ctx, ui_language="zh-CN")
        template = self._template(product_id)
        self.assertEqual(len(template["rows"]), 3 + 42)
        self.assertEqual([c["label"] for c in template["columns"]][:3], ["素材编号", "方向", "画面组合"])
        self.assertTrue(template["file_name"].endswith("投放结果模板.csv"))
        titles = {direction["title"] for direction in plan["directions"]}
        for row in template["rows"]:
            self.assertRegex(row["asset_code"], r"^MK-[0-9A-HJKMNP-TV-Z]{6}$")
            self.assertTrue(row["attributable"])
            self.assertEqual(row["round_number"], 1)
            self.assertIn(row["direction"], titles)
            self.assertEqual(len(row["combination"].split(" · ")), 4, row["combination"])  # direction + 3 factors
            self.assertEqual(row["evidence_mode"], "mock")
        self.assertEqual(len({row["asset_code"] for row in template["rows"]}), 45)

    def test_too_many_rows_carries_its_limit_like_the_csv_preflight(self):
        # One code, one wording (#253, #255): both checks send {limit}.
        many = [{"asset_code": "MK-000000", "date_start": START, "date_end": END, "impressions": 1, "clicks": 0}]
        with self.assertRaises(parsing.ParseError) as caught:
            parsing.rows_from_manual(many * (parsing.MAX_ROWS + 1))
        self.assertEqual((caught.exception.code, caught.exception.params), ("too_many_rows", {"limit": parsing.MAX_ROWS}))
        header = "素材编号,开始日期,结束日期,展示次数,链接点击量\n"
        with self.assertRaises(parsing.ParseError) as caught:
            parsing.parse_table(header + f"MK-000000,{START},{END},1,0\n" * (parsing.MAX_ROWS + 1))
        self.assertEqual(caught.exception.params, {"limit": parsing.MAX_ROWS})

    def test_the_template_is_headed_in_the_interface_language_and_both_parse_back(self):
        # A downloaded file follows X-UI-Language (#255); plan prose in the rows does not change.
        _plan, product_id = self._generated()
        self.current = replace(self.ctx, ui_language="zh-CN")
        chinese = self._template(product_id)
        self.current = replace(self.ctx, ui_language="en")
        english = self._template(product_id)
        self.assertEqual([c["key"] for c in english["columns"]], [c["key"] for c in chinese["columns"]])
        self.assertEqual([c["label"] for c in english["columns"]][:5], ["Asset code", "Direction", "Combination", "Ad name", "Ad ID"])
        self.assertTrue(english["file_name"].endswith("-results-template.csv"), english["file_name"])
        self.assertIsNone(CJK.search(" ".join(c["label"] for c in english["columns"])))
        self.assertEqual(english["rows"], chinese["rows"])
        for language, headers in parsing.TEMPLATE_HEADERS.items():
            table = parsing.parse_table(",".join(label for _, label in headers) + "\n" + ",".join("1" for _ in headers) + "\n")
            self.assertEqual(set(table.columns), {key for key, _ in headers} - {"direction", "combination"}, language)
            self.assertEqual(len(table.ignored_columns), 2, language)

    def test_an_external_upload_is_reported_as_unattributable(self):
        _plan, product_id = self._generated()
        with tenant_session(self.ctx) as session:
            existing = session.scalars(select(AssetRecord).where(AssetRecord.product_id == product_id)).first()
            asset = AssetRecord(
                id=f"asset_ext_{uuid.uuid4().hex[:8]}", workspace_id=self.ctx.workspace_id, product_id=product_id,
                run_id=existing.run_id, combo_id="external", combo_code="external", tags_json={},
                filename="ugc.jpg", provider="upload", model="", prompt="", fingerprint=uuid.uuid4().hex,
                status="succeeded",
            )
            session.add(asset)
            session.flush()
            version = AssetVersionRecord(
                id=f"av_ext_{uuid.uuid4().hex[:8]}", workspace_id=self.ctx.workspace_id, asset_id=asset.id,
                kind="image", aspect_ratio="1:1", object_key="k", content_sha256=uuid.uuid4().hex * 2,
                byte_size=10, mime_type="image/png", origin_kind="upload", execution_mode=None,
            )
            session.add(version)
            session.flush()
            version_id = version.id
        code = codes.asset_code(self.ctx.workspace_id, version_id)
        row = next(item for item in self._template(product_id)["rows"] if item["asset_code"] == code)
        self.assertFalse(row["attributable"])
        self.assertEqual((row["direction"], row["combination"]), ("External asset", "External asset"))
        self.current = replace(self.ctx, ui_language="zh-CN")
        row = next(item for item in self._template(product_id)["rows"] if item["asset_code"] == code)
        self.assertEqual((row["direction"], row["combination"]), ("外部素材", "外部素材"))
        preview = self._import({
            "product_id": product_id, "source": "manual", "currency": "USD",
            "rows": [{"asset_code": code, "date_start": START, "date_end": END, "impressions": 3000, "clicks": 40}],
        })
        self.assertEqual(preview["rows"][0]["status"], "unattributable")
        self.assertIn("外部素材还没有打标签", preview["rows"][0]["reason"])
        # The browser words the row from codes (#255); the stored text is unchanged.
        self.assertEqual((preview["rows"][0]["reason_code"], preview["rows"][0]["reason_params"]), ("external_untagged", None))
        self.assertEqual((preview["rows"][0]["description"], preview["rows"][0]["description_code"]), ("外部素材", "external_asset"))
        self.assertEqual(preview["rows"][0]["evidence_mode"], "production")
        committed = self._commit(preview["import_id"])
        self.assertEqual(committed["status"], "committed")
        learnings = self._learnings(product_id)
        self.assertEqual(learnings["status"], "no_data")
        self.assertEqual(learnings["evidence_mode"], "production")
        self.assertEqual(learnings["unattributable"][0]["asset_code"], code)
        self.assertEqual(learnings["unattributable"][0]["reason_code"], "external_untagged")


class TestImportAndLearnings(LearningsFixtures):
    def test_a_csv_yields_value_verdicts_and_a_one_click_next_round(self):
        """Contract §7-3: import a CSV -> "有人 > 无人" conclusions -> next round."""
        plan, product_id = self._generated()
        self._make_byok(product_id)
        template = self._template(product_id)
        self.assertTrue(all(row["evidence_mode"] == "production" for row in template["rows"]))
        before = self._learnings(product_id)
        self.assertEqual(before["status"], "no_data")
        self.assertIsNone(before["snapshot_id"])
        self.assertIsNotNone(before["review_signals"])
        self.assertEqual(before["review_signals"]["note"], "这是人工审核的弱信号，不是投放数据")

        preview = self._import({
            "product_id": product_id, "source": "csv", "csv_text": self._csv(template), "file_name": "九月.csv",
        })
        self.assertEqual(preview["status"], "previewed")
        self.assertEqual(preview["summary"]["by_status"]["ready"], 45)
        self.assertEqual(preview["summary"]["evidence_modes"], {"production": 45})
        self.assertIn("方向", preview["summary"]["ignored_columns"])
        first = preview["rows"][0]
        self.assertEqual(first["round_number"], 1)
        self.assertIn(" · ", first["description"])
        self.assertEqual(self._observations(), 0, "preview writes no evidence")

        committed = self._commit(preview["import_id"])
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(len(committed["snapshot_ids"]), 1)

        learnings = self._learnings(product_id)
        self.assertEqual(learnings["status"], "ready")
        self.assertEqual(learnings["evidence_mode"], "production")
        self.assertIsNone(learnings["evidence_notice"])
        self.assertEqual(learnings["snapshot_id"], committed["snapshot_ids"][0])
        self.assertEqual(learnings["thresholds"]["min_impressions"], "1000")
        round_ = learnings["rounds"][0]
        self.assertEqual(round_["label"], "第 1 轮")
        self.assertEqual(round_["totals"]["assets"], 45)
        people = self._value(round_, "人物", "真人使用")
        self.assertEqual(people["verdict"], "winner", people["sentence"])
        self.assertTrue(people["sentence"].startswith("真人使用 比 纯产品 CTR 高 "), people["sentence"])
        self.assertIn("95% 区间", people["sentence"])
        self.assertIn("次展示 / 23 张素材", people["sentence"])
        self.assertIn("CPA 差异尚不显著", people["sentence"])
        self.assertEqual(self._value(round_, "人物", "纯产品")["verdict"], "loser")
        # Scenes are tied to directions by the plan's design -> withheld.
        scene = self._value(round_, "场景", "居家日常")
        self.assertEqual(scene["verdict"], "insufficient_data")
        self.assertIn("confounded", scene["flags"])
        self.assertEqual(self._value(round_, "文案切入", "痛点对比")["verdict"], "no_difference")
        self.assertEqual(len(round_["directions"]), 3)
        self.assertTrue(all(d["n"]["assets"] > 0 for d in round_["directions"]))
        self.assertIn(people["sentence"], learnings["highlights"])
        self.assertTrue(learnings["can_seed_next_round"])
        self.assertEqual(learnings["recent_imports"][0]["file_name"], "九月.csv")

        results = self.client.get(f"/api/v1/test-plans/{plan['plan_id']}/results").json()
        self.assertEqual(results["status"], "ready")
        self.assertEqual(results["round"]["plan_id"], plan["plan_id"])
        self.assertTrue(results["can_seed_next_round"])

        # 按结论生成下一轮
        accepted = self._create({
            "product_id": product_id, "goal": "optimize",
            "from_learnings_snapshot_id": learnings["snapshot_id"],
        })
        self._drain()
        nxt = self._get(accepted["plan_id"])
        self.assertEqual(nxt["status"], "ready")
        self.assertEqual(nxt["goal"], "optimize")
        basis = nxt["learnings_basis"]
        self.assertEqual(basis["snapshot_id"], learnings["snapshot_id"])
        self.assertTrue(basis["explicit"])
        self.assertIn("人物：真人使用", basis["winners"])
        self.assertIn("人物：纯产品", basis["losers"])
        with tenant_session(self.ctx) as session:
            seeds = session.get(AdTestPlanRecord, accepted["plan_id"]).recipe_json["seeds"]
        winners = [v for v in seeds["value_verdicts"] if v["verdict"] == "winner"]
        self.assertIn(("人物", "真人使用"), {(v["factor_label"], v["value_label"]) for v in winners})
        # The mock drafter skips the loser everywhere but the exploratory last direction.
        people_factor = next(f for f in nxt["factors"] if f["key"] == "people")
        loser_id = next(v["id"] for v in people_factor["values"] if v["label"] == "纯产品")
        directions = nxt["directions"]
        self.assertTrue(all(loser_id not in d["factor_values"]["people"] for d in directions[:-1]))
        self.assertIn(loser_id, directions[-1]["factor_values"]["people"])
        self.assertIn("探索方向", directions[-1]["what_it_tests"])
        self.assertIn("沿用上一轮胜出的「真人使用」", directions[0]["hypothesis"])

        # A plain new plan still receives the latest production learnings.
        plain = self._create({"product_id": product_id})
        self._drain()
        self.assertFalse(self._get(plain["plan_id"])["learnings_basis"]["explicit"])
        self.assert_stored_widths()

    def test_the_same_rows_are_never_imported_twice(self):
        _plan, product_id = self._generated()
        text = self._csv(self._template(product_id))
        first = self._import({"product_id": product_id, "source": "csv", "csv_text": text}, key="ik-r1")
        replay = self._import({"product_id": product_id, "source": "csv", "csv_text": text}, key="ik-r1")
        self.assertEqual(replay["import_id"], first["import_id"])
        self.assertTrue(replay["replayed"])
        conflict = self._import({"product_id": product_id, "source": "csv", "csv_text": text + "\n"}, key="ik-r1", expect=409)
        self.assertEqual(conflict["code"], "idempotency_key_reused")
        # The same content reordered under a new key is the same import.
        lines = text.strip().splitlines()
        shuffled = "\n".join([lines[0]] + list(reversed(lines[1:])))
        again = self._import({"product_id": product_id, "source": "csv", "csv_text": shuffled})
        self.assertEqual(again["import_id"], first["import_id"])
        self.assertTrue(again["duplicate"])
        committed = self._commit(first["import_id"])
        twice = self._commit(first["import_id"])
        self.assertTrue(twice["replayed"])
        self.assertEqual(twice["snapshot_ids"], committed["snapshot_ids"])
        after = self._import({"product_id": product_id, "source": "csv", "csv_text": text})
        self.assertEqual(after["status"], "committed")
        self.assertTrue(after["duplicate"])
        self.assertEqual(self._observations(), 45)
        self.assertEqual(len(self._audits("result_import.committed")), 1)
        self.assertEqual(len(self._audits("learning_snapshot.computed")), 1)

    def test_rows_are_validated_and_matched_with_reasons(self):
        _plan, product_id = self._generated()
        other_product = self._product()
        rows = self._template(product_id)["rows"]
        a, b = rows[0]["asset_code"], rows[1]["asset_code"]
        future = (TODAY + timedelta(days=3)).isoformat()
        other_ws = self._make_context(role=ROLE_OWNER, actor="owner_b")
        self.addCleanup(cancel_workspace_jobs, other_ws.workspace_id)
        foreign_code = codes.asset_code(other_ws.workspace_id, "av_x")
        text = "\n".join([
            "素材编号,广告名称,开始日期,结束日期,展示次数,链接点击量,花费,币种",
            f"{a},,{START},{END},1000,20,5,USD",
            f"{a},,{START},{END},1000,20,5,USD",                 # duplicate_in_file
            f"{b},,{START},{future},1000,20,5,USD",              # future_date
            f"{b},,2026/13/40,{END},1000,20,5,USD",             # invalid_date
            f"{b},,{START},{END},10,20,5,USD",                  # clicks_exceed_impressions
            f"{b},,{START},{END},1000,20,5,",                   # currency_missing (no default)
            f"{foreign_code},,{START},{END},1000,20,5,USD",     # unknown_code (another Workspace)
            f",夏季_{b}_v2,{START},{END},1000,20,5,EUR",         # matched by ad name; currency_mismatch
            f",随便,{START},{END},1000,20,5,USD",                # no code in the ad name
        ])
        preview = self._import({"product_id": product_id, "source": "csv", "csv_text": text})
        reasons = {row["line"]: row["reason_code"] for row in preview["rows"]}
        self.assertEqual(preview["rows"][0]["status"], "ready")
        self.assertEqual(reasons[3], "duplicate_in_file")
        self.assertEqual(reasons[4], "future_date")
        self.assertEqual(reasons[5], "invalid_date")
        self.assertEqual(reasons[6], "clicks_exceed_impressions")
        self.assertEqual(reasons[7], "currency_missing")
        self.assertEqual(reasons[8], "unknown_code")
        self.assertEqual(reasons[9], "currency_mismatch")
        params = {row["line"]: row["reason_params"] for row in preview["rows"]}
        self.assertEqual(params[9], {"expected": "USD", "found": "EUR"})
        self.assertTrue(all(params[line] is None for line in params if line != 9), params)
        self.assertEqual(reasons[10], "unknown_code")
        self.assertEqual(preview["summary"]["by_status"]["rejected"], 8)
        self.assertTrue(all(row["reason"] for row in preview["rows"] if row["status"] == "rejected"))

        # Another product's image in this product's sheet.
        _plan_b, _ = self._generated(product_id=other_product)
        foreign = self._template(other_product)["rows"][0]["asset_code"]
        refused = self._import({
            "product_id": product_id, "source": "manual", "currency": "USD",
            "rows": [{"asset_code": foreign, "date_start": START, "date_end": END, "impressions": 100, "clicks": 1}],
        })
        self.assertEqual(refused["rows"][0]["reason_code"], "other_product")
        nothing = self._commit(refused["import_id"], expect=422)
        self.assertEqual(nothing["code"], "nothing_to_import")

        bad = self._import({"product_id": product_id, "source": "csv", "csv_text": "开始日期,结束日期\n1,2\n"}, expect=422)
        self.assertEqual(bad["code"], "missing_identifier_column")
        bad = self._import({"product_id": product_id, "source": "csv"}, expect=422)
        self.assertEqual(bad["code"], "csv_input_invalid")

    def test_overlaps_are_refused_and_the_same_range_replaces(self):
        _plan, product_id = self._generated()
        self._make_byok(product_id)
        template = self._template(product_id)
        code = template["rows"][0]["asset_code"]

        def manual(start, end, impressions):
            return {"product_id": product_id, "source": "manual", "currency": "USD", "rows": [
                {"asset_code": code, "date_start": start, "date_end": end, "impressions": impressions, "clicks": 10},
            ]}

        self._commit(self._import(manual(START, END, 1000))["import_id"])
        overlap_start = (TODAY - timedelta(days=10)).isoformat()
        overlap = self._import(manual(overlap_start, (TODAY - timedelta(days=2)).isoformat(), 900))
        self.assertEqual(overlap["rows"][0]["reason_code"], "overlaps_existing")
        replacing = self._import(manual(START, END, 1500))
        self.assertEqual(replacing["rows"][0]["status"], "replaces")
        self._commit(replacing["import_id"])
        later = self._import(manual((TODAY - timedelta(days=7)).isoformat(), (TODAY - timedelta(days=1)).isoformat(), 500))
        self.assertEqual(later["rows"][0]["status"], "ready")
        self._commit(later["import_id"])
        learnings = self._learnings(product_id)
        self.assertEqual(learnings["rounds"][0]["totals"]["impressions"], 1500 + 500, "the corrected range counts once")
        self.assertEqual(self._observations(), 3, "append-only history")

    def test_a_stale_preview_is_refreshed_and_refused(self):
        _plan, product_id = self._generated()
        code = self._template(product_id)["rows"][0]["asset_code"]
        row = {"asset_code": code, "date_start": START, "date_end": END, "impressions": 1000, "clicks": 10}
        first = self._import({"product_id": product_id, "source": "manual", "rows": [row]})
        second = self._import({"product_id": product_id, "source": "manual", "rows": [{**row, "clicks": 12}]})
        self.assertEqual(second["rows"][0]["status"], "ready")
        self._commit(first["import_id"])
        stale = self._commit(second["import_id"], expect=409)
        self.assertEqual(stale["code"], "preview_stale")
        refreshed = self.client.get(f"/api/v1/results/imports/{second['import_id']}").json()
        self.assertEqual(refreshed["rows"][0]["status"], "replaces")
        self.assertEqual(self._commit(second["import_id"])["status"], "committed")

    def test_insufficient_data_never_concludes_and_cannot_seed_a_round(self):
        _plan, product_id = self._generated()
        self._make_byok(product_id)
        template = self._template(product_id)
        preview = self._import({
            "product_id": product_id, "source": "csv",
            "csv_text": self._csv(template, impressions=30, conversions=0),
        })
        committed = self._commit(preview["import_id"])
        learnings = self._learnings(product_id)
        self.assertEqual(learnings["status"], "ready")
        self.assertFalse(learnings["can_seed_next_round"])
        self.assertEqual(learnings["highlights"], [])
        people = self._value(learnings["rounds"][0], "人物", "真人使用")
        self.assertEqual(people["verdict"], "insufficient_data")
        self.assertIn("样本不足，尚不能下结论", people["sentence"])
        refused = self._create(
            {"product_id": product_id, "goal": "optimize", "from_learnings_snapshot_id": committed["snapshot_ids"][0]},
            expect=409,
        )
        self.assertEqual(refused["code"], "learnings_inconclusive")


class TestLlmDrafterReceivesLearnings(LearningsFixtures):
    """#164's governed LLM drafter sees the value verdicts in its prompt."""

    def test_prompt_lists_learned_winners_losers_and_flat_values(self):
        verdicts = drafting.WinnersLosers(value_verdicts=[
            drafting.ValueVerdict("人物", "真人使用", "winner", "ctr", sentence="真人使用 比 纯产品 CTR 高 78%"),
            drafting.ValueVerdict("人物", "纯产品", "loser", "ctr", sentence="纯产品 比 真人使用 CTR 低 44%"),
            drafting.ValueVerdict("文案切入", "痛点对比", "no_difference", "ctr", sentence="差异不显著"),
        ])
        facts = drafting.ProductFacts(product_id="p", entry_id="e", version_id="v", name="VoltNest")
        prompt = llm_drafter.build_user_prompt(
            facts, "optimize", drafting.AudienceChoice(text="车主"), "sample", verdicts,
        )
        winners, rest = prompt.split("落败的取值")
        losers, flat = rest.split("差异不显著的取值")
        self.assertIn("- 人物 = 真人使用：真人使用 比 纯产品 CTR 高 78%", winners)
        self.assertIn("- 人物 = 纯产品", losers)
        self.assertIn("- 文案切入 = 痛点对比", flat)
        self.assertNotIn("基线素材", prompt, "a next round from learnings has no seed assets")
        self.assertNotIn("暂无投放结论", prompt)

    def test_a_byok_next_round_sends_the_learned_labels_to_the_llm(self):
        _plan, product_id = self._generated()
        self._make_byok(product_id)
        preview = self._import({"product_id": product_id, "source": "csv", "csv_text": self._csv(self._template(product_id))})
        snapshot_id = self._commit(preview["import_id"])["snapshot_ids"][0]

        self._bind_openai()
        self._use_fake_openai(openai_completion(json.dumps(TEST_PLAN_DRAFT_EXAMPLE, ensure_ascii=False)))
        accepted = self._create({
            "product_id": product_id, "goal": "optimize", "from_learnings_snapshot_id": snapshot_id,
        })
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "ready", plan.get("failure"))
        self.assertEqual(plan["drafting"]["drafter"], "llm")
        self.assertEqual(len(self.openai_requests), 1)
        user = json.loads(self.openai_requests[0].content)["messages"][-1]["content"]
        winners, losers = user.split("落败的取值")
        self.assertIn("- 人物 = 真人使用：真人使用 比 纯产品 CTR 高", winners)
        self.assertIn("- 人物 = 纯产品", losers)
        self.assertIn("exploratory", user)


class TestEvidencePartitions(LearningsFixtures):
    def test_mock_images_stay_in_a_flagged_demo_partition(self):
        _plan, product_id = self._generated()
        template = self._template(product_id)
        preview = self._import({"product_id": product_id, "source": "csv", "csv_text": self._csv(template)})
        self.assertEqual(preview["summary"]["evidence_modes"], {"mock": 45})
        committed = self._commit(preview["import_id"])
        learnings = self._learnings(product_id)
        self.assertEqual(learnings["evidence_mode"], "mock")
        self.assertIn("演示数据", learnings["evidence_notice"])
        self.assertEqual(learnings["available_modes"], ["mock"])
        self.assertEqual(self._learnings(product_id, evidence_mode="production")["status"], "no_data")
        with tenant_session(self.ctx) as session:
            snapshot = session.get(LearningSnapshotRecord, committed["snapshot_ids"][0])
            self.assertEqual(snapshot.evidence_mode, "mock")

        # Mock learnings never seed a plan on their own ...
        plain = self._create({"product_id": product_id})
        self._drain()
        self.assertIsNone(self._get(plain["plan_id"])["learnings_basis"])
        # ... only when the user explicitly starts the next round from them.
        explicit = self._create({
            "product_id": product_id, "goal": "optimize", "from_learnings_snapshot_id": committed["snapshot_ids"][0],
        })
        self._drain()
        basis = self._get(explicit["plan_id"])["learnings_basis"]
        self.assertEqual(basis["evidence_mode"], "mock")

    def test_identical_inputs_reuse_the_snapshot(self):
        _plan, product_id = self._generated()
        from app.learnings import service as learnings_service

        preview = self._import({"product_id": product_id, "source": "csv", "csv_text": self._csv(self._template(product_id))})
        snapshot_id = self._commit(preview["import_id"])["snapshot_ids"][0]
        with tenant_session(self.ctx) as session:
            again = learnings_service.compute_snapshot(session, self.ctx, product_id, "mock")
            self.assertEqual(again.id, snapshot_id)

    def test_meta_synced_ads_bound_to_images_import_read_only(self):
        _plan, product_id = self._generated()
        self._make_byok(product_id)
        template = self._template(product_id)
        av_ids = _asset_version_ids(self.ctx, product_id)[:4]
        with tenant_session(self.ctx) as session:
            account = MetaAccountRecord(
                id=f"macct_{uuid.uuid4().hex[:8]}", workspace_id=self.ctx.workspace_id,
                ad_account_id="act_42", name="test", currency="USD", api_version="v25.0",
            )
            session.add(account)
            session.add(MetaConnectionRecord(
                id=f"mconn_{uuid.uuid4().hex[:8]}", workspace_id=self.ctx.workspace_id,
                meta_mode="internal_validation", ad_account_id="act_42", state="active",
            ))
            sync = MetaSyncRunRecord(
                id=f"msync_{uuid.uuid4().hex[:8]}", workspace_id=self.ctx.workspace_id, account_id=account.id,
                state="succeeded", date_start=TODAY - timedelta(days=7), date_stop=TODAY - timedelta(days=1),
                contract_hash="c" * 64, contract_json={}, api_version="v25.0", idempotency_key=uuid.uuid4().hex,
            )
            session.add(sync)
            session.flush()
            for index, av_id in enumerate(av_ids):
                ad_id = f"2384{index:04d}"
                session.add(MetaAdBindingRecord(
                    id=f"mab_{uuid.uuid4().hex[:8]}", account_id=account.id, ad_id=ad_id, asset_version_id=av_id,
                ))
                for day in range(3):
                    session.add(AdDailyMetricRecord(
                        id=f"adm_{uuid.uuid4().hex[:10]}", account_id=account.id, sync_run_id=sync.id,
                        ad_id=ad_id, metric_date=TODAY - timedelta(days=3 + day), impressions=800, clicks=12,
                        spend="9.5", purchases="1", purchase_value="30", currency="USD",
                        contract_hash="c" * 64, raw_json={},
                    ))
        preview = self._import({"product_id": product_id, "source": "meta_sync"})
        self.assertEqual(preview["source"], "meta_sync")
        self.assertEqual(preview["summary"]["by_status"]["ready"], 12)
        self.assertEqual(preview["summary"]["evidence_modes"], {"production": 12})
        self.assertTrue(all(row["input"]["ad_id"].startswith("2384") for row in preview["rows"]))
        committed = self._commit(preview["import_id"])
        self.assertEqual(committed["status"], "committed")
        # Nothing new synced -> the same import comes back.
        again = self._import({"product_id": product_id, "source": "meta_sync"})
        self.assertTrue(again["duplicate"])
        # The same ad id in a CSV matches through the binding, and overlaps the synced days.
        text = "\n".join([
            "广告编号,开始日期,结束日期,展示次数,链接点击量",
            f"23840000,{(TODAY - timedelta(days=5)).isoformat()},{(TODAY - timedelta(days=3)).isoformat()},100,1",
            f"23840001,{(TODAY - timedelta(days=30)).isoformat()},{(TODAY - timedelta(days=20)).isoformat()},100,1",
            f"99999999,{START},{END},100,1",
        ])
        csv_preview = self._import({"product_id": product_id, "source": "csv", "csv_text": text})
        statuses = [(row["status"], row["reason_code"]) for row in csv_preview["rows"]]
        self.assertEqual(statuses[0], ("rejected", "overlaps_existing"))
        self.assertEqual(statuses[1][0], "ready")
        self.assertEqual(statuses[2], ("rejected", "unknown_ad"))
        self.assertTrue(template["rows"])
        self.assert_stored_widths()

    def test_meta_rows_from_a_mock_connection_are_mock_evidence(self):
        _plan, product_id = self._generated()
        self._make_byok(product_id)
        av_id = _asset_version_ids(self.ctx, product_id)[0]
        with tenant_session(self.ctx) as session:
            account = MetaAccountRecord(
                id=f"macct_{uuid.uuid4().hex[:8]}", workspace_id=self.ctx.workspace_id,
                ad_account_id="act_mock", currency="USD", api_version="v25.0",
            )
            session.add(account)
            session.add(MetaConnectionRecord(
                id=f"mconn_{uuid.uuid4().hex[:8]}", workspace_id=self.ctx.workspace_id,
                meta_mode="controlled_mock", ad_account_id="act_mock", state="active",
            ))
            sync = MetaSyncRunRecord(
                id=f"msync_{uuid.uuid4().hex[:8]}", workspace_id=self.ctx.workspace_id, account_id=account.id,
                state="succeeded", date_start=TODAY - timedelta(days=2), date_stop=TODAY - timedelta(days=2),
                contract_hash="c" * 64, contract_json={}, api_version="v25.0", idempotency_key=uuid.uuid4().hex,
            )
            session.add(sync)
            session.flush()
            session.add(MetaAdBindingRecord(id=f"mab_{uuid.uuid4().hex[:8]}", account_id=account.id, ad_id="777", asset_version_id=av_id))
            session.add(AdDailyMetricRecord(
                id=f"adm_{uuid.uuid4().hex[:10]}", account_id=account.id, sync_run_id=sync.id, ad_id="777",
                metric_date=TODAY - timedelta(days=2), impressions=500, clicks=5, spend="3", purchases="0",
                purchase_value="0", currency="USD", contract_hash="c" * 64, raw_json={},
            ))
        preview = self._import({"product_id": product_id, "source": "meta_sync"})
        self.assertEqual(preview["summary"]["evidence_modes"], {"mock": 1})


class TestUploadedFile(LearningsFixtures):
    def test_an_uploaded_gb18030_csv_is_read_from_the_committed_blob(self):
        _plan, product_id = self._generated()
        text = self._csv(self._template(product_id))

        class FakeStore:
            async def open_committed(self, ref):
                self.ref = ref
                yield text.encode("gb18030")

        fake = FakeStore()
        resolved = {
            "upload_intent_id": "ui_test", "committed_key": "k", "storage_version_id": "v",
            "checksum_sha256_b64": "x", "content_type": "text/csv", "size": 10, "file_name": "结果.csv",
        }
        with patch("app.learnings.v1_router._blob_store", fake), \
                patch("app.learnings.service.resolve_upload", lambda ctx, intent_id: resolved):
            preview = self._import({"product_id": product_id, "source": "csv", "upload_intent_id": "ui_test"})
        self.assertEqual(preview["file_name"], "结果.csv")
        self.assertEqual(preview["summary"]["by_status"]["ready"], 45)
        self.assertEqual(fake.ref.committed_key, "k")

    def test_an_upload_of_another_purpose_or_unknown_is_refused(self):
        product_id = self._product()
        refused = self._import({"product_id": product_id, "source": "csv", "upload_intent_id": "ui_missing"}, expect=404)
        self.assertEqual(refused["code"], "upload_intent_not_found")


class TestTenancyAndPermissions(LearningsFixtures):
    def test_another_workspace_sees_nothing(self):
        plan, product_id = self._generated()
        preview = self._import({"product_id": product_id, "source": "csv", "csv_text": self._csv(self._template(product_id))})
        other = self._make_context(role=ROLE_OWNER, actor="owner_b")
        self.addCleanup(cancel_workspace_jobs, other.workspace_id)
        self.current = other
        for response, code in (
            (self.client.get(f"/api/v1/results/imports/{preview['import_id']}"), "result_import_not_found"),
            (self.client.post(f"/api/v1/results/imports/{preview['import_id']}/commit", headers=self._headers()),
             "result_import_not_found"),
            (self.client.get(f"/api/v1/products/{product_id}/learnings"), "product_not_found"),
            (self.client.get(f"/api/v1/products/{product_id}/results/template"), "product_not_found"),
            (self.client.get(f"/api/v1/test-plans/{plan['plan_id']}/results"), "test_plan_not_found"),
            (self.client.post("/api/v1/results/imports", headers=self._headers(), json={
                "product_id": product_id, "source": "manual",
                "rows": [{"asset_code": "MK-000000", "date_start": START, "date_end": END, "impressions": 1, "clicks": 0}],
            }), "product_not_found"),
        ):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["code"], code)
        # Another Workspace's snapshot cannot seed this Workspace's plan.
        self.current = self.ctx
        committed = self._commit(preview["import_id"])
        self.current = other
        other_product = self._product(other)
        refused = self._create({
            "product_id": other_product, "goal": "optimize", "from_learnings_snapshot_id": committed["snapshot_ids"][0],
        }, expect=404)
        self.assertEqual(refused["code"], "learnings_snapshot_not_found")

    def test_owners_and_members_import_but_a_system_identity_cannot(self):
        _plan, product_id = self._generated()
        owner = self.owner_ctx(self.ctx)
        self.current = owner
        preview = self._import({"product_id": product_id, "source": "csv", "csv_text": self._csv(self._template(product_id))})
        self.current = self.ctx
        self.assertEqual(self._commit(preview["import_id"])["status"], "committed")
        system = TenantContext(
            workspace_id=self.ctx.workspace_id, role="system", executor="system:worker",
            initiator_id="member_a", initiator_role="member",
        )
        self.current = system
        for response in (
            self.client.get(f"/api/v1/products/{product_id}/learnings"),
            self.client.get(f"/api/v1/results/imports/{preview['import_id']}"),
            self.client.post(f"/api/v1/results/imports/{preview['import_id']}/commit", headers=self._headers()),
        ):
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["code"], "learnings_forbidden")

    def test_commands_need_an_idempotency_key(self):
        product_id = self._product()
        response = self.client.post("/api/v1/results/imports", json={"product_id": product_id, "source": "meta_sync"})
        self.assertIn(response.status_code, (400, 422), response.text)


def _asset_version_ids(ctx: TenantContext, product_id: str) -> list[str]:
    with tenant_session(ctx) as session:
        return list(session.scalars(
            select(AssetVersionRecord.id)
            .join(AssetRecord, AssetRecord.id == AssetVersionRecord.asset_id)
            .where(AssetRecord.product_id == product_id)
            .order_by(AssetVersionRecord.id)
        ))


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
