"""J-44 (#255): an export package is written in the requester's interface language.

The ZIP's stable keys -- entry ids, ASCII Meta ad names, media paths and the
metrics template -- never depend on that language; MarketAI's own words do,
and the language is frozen into the manifest when the export is requested.
"""
import asyncio
import copy
import csv
import io
import json
import re
import zipfile
from dataclasses import replace

from app.adapters.persistence.database import tenant_session
from app.marketing import workflow
from app.marketing.handoff_worker import execute_export, package_bytes
from app.marketing.languages import CJK
from app.marketing.workflow_contracts import CreateHandoff
from app.marketing.workflow_models import MarketingHandoffRecord
from app.platform.blob_store import CommittedBlobRef
from tests.test_marketing_workflow import MarketingWorkflowFixtures

AD_NAME = re.compile(r"^MA-[0-9a-f]{12} [ -~]+ / [ -~]+ #\d+(v\d+)?$")
QUERY = "include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true"


class _Rollback(Exception):
    pass


class ExportLanguageFixtures(MarketingWorkflowFixtures):
    SOURCE_TEXT = "Alpha charger\n65W, three ports, for business travellers."
    STRATEGY_LANGUAGE = "en-US"
    AD_LANGUAGE = "en-US"

    def create(self):
        response = self.client.post(f"/api/v1/marketing/projects?{QUERY}", headers=self.headers(),
            json={"source_text": self.SOURCE_TEXT})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def plan_call(self, project, kind="understanding", mode="mock", quote_hash="", key=None):
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning",
            headers=self.headers(project["revision"], key), json={"kind": kind, "execution_mode": mode, "quote_hash": quote_hash,
                **({"ad_language": self.AD_LANGUAGE, "strategy_language": self.STRATEGY_LANGUAGE} if kind == "plan" else {})})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def speak(self, language):
        """Later requests arrive with this X-UI-Language (the tenant and actor are unchanged)."""
        self.context = replace(self.context, ui_language=language)

    def approved_round(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        for asset in row["assets"]:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Checked product, proposition, copy and comparison"}, 201)
        return project, row

    def export(self, row):
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]}, 202)
        asyncio.run(execute_export(self.context, row["handoff"]["id"], self.blobs))
        return self.read_round(row["id"])

    def read_round(self, round_id, **flags):
        """The round as the current web reads it: #255's fields are opt-in (strict older clients)."""
        query = {"include_text_language": "true", **flags}
        return self.client.get(f"/api/v1/marketing/rounds/{round_id}", params=query).json()["data"]

    def package(self, row):
        with tenant_session(self.context) as session:
            ref = CommittedBlobRef(**session.get(MarketingHandoffRecord, row["handoff"]["id"]).blob_json)
        return asyncio.run(self.blobs.read_committed(ref, 10_000_000))

    def files(self, content):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def ads(self, files):
        return list(csv.reader(io.StringIO(files["ads.csv"].decode("utf-8-sig"))))

    def text_files(self, files):
        return {name: data.decode("utf-8-sig") for name, data in files.items() if not name.startswith("media/")}


class TestEnglishPackage(ExportLanguageFixtures):
    def test_an_english_interface_exports_an_all_english_package_with_ascii_ad_names(self):
        _, row = self.approved_round()
        row = self.export(row)
        self.assertEqual(row["handoff"]["state"], "ready")
        self.assertEqual(row["handoff"]["language"], "en")
        files = self.files(self.package(row))

        self.assertTrue(all(name.isascii() for name in files), sorted(files))
        for name, text in self.text_files(files).items():
            self.assertIsNone(CJK.search(text), f"{name} contains CJK text")
        manifest = json.loads(files["manifest.json"])
        self.assertEqual((manifest["schema_version"], manifest["language"]), (2, "en"))
        header, *rows = self.ads(files)
        self.assertEqual(header, ["entry_id", "ad_name", "display_name", "file", "headline", "body_copy", "group", "combination"])
        self.assertEqual(len(rows), 5)
        for entry in manifest["entries"]:
            self.assertRegex(entry["ad_name"], AD_NAME)
            self.assertTrue(entry["display_name"].endswith(" · Execution 1"), entry["display_name"])
        self.assertIn("Keep the MA key in each ad name", files["README.txt"].decode())
        self.assertEqual(files["daily-metrics-template.csv"].decode("utf-8-sig").strip(),
                         "date,ad_id,spend,impressions,clicks,purchases,purchase_value")


class TestChinesePlanPackage(ExportLanguageFixtures):
    SOURCE_TEXT = "Alpha 充电器\n65W，多口，面向差旅办公用户。"
    STRATEGY_LANGUAGE = "zh-CN"

    def test_a_chinese_plan_still_gets_ascii_ad_names_from_the_english_image_brief(self):
        # Before #255 the ad name was the plan's Chinese proposition and expression.
        project, row = self.approved_round()
        self.assertTrue(CJK.search(project["plan"]["angles"][0]["proposition"]))
        self.speak("zh-CN")
        row = self.export(row)
        self.assertEqual(row["handoff"]["language"], "zh-CN")
        briefs = {c["id"]: c["image_brief"] for c in project["plan"]["combinations"]}
        for entry in row["handoff"]["entries"]:
            self.assertRegex(entry["ad_name"], AD_NAME)
            brief = briefs[entry["combination_id"]]
            self.assertIn(f" {brief['proposition']} / {brief['expression']} #1", entry["ad_name"])
            self.assertEqual(entry["display_name"], f"{entry['proposition']} / {entry['expression_name']} · 执行 1")

    def test_a_chinese_interface_exports_chinese_notes_over_the_same_stable_keys(self):
        _, row = self.approved_round()
        self.speak("zh-CN")
        files = self.files(self.package(self.export(row)))

        self.assertTrue(all(name.isascii() for name in files), "file names are stable keys")
        readme = files["README.txt"].decode()
        self.assertIn("保留每个广告名称里的 MA 编号", readme)
        self.assertIn("归因口径使用点击后 7 天 / 浏览后 1 天（7d_click_1d_view）", readme)
        self.assertIn("模式：示例（受控 Mock）", readme)
        self.assertEqual(self.ads(files)[0], ["条目 ID", "广告名称", "显示名称", "文件", "标题", "正文", "测试组", "创意组合"])
        # The CSV import reads the template back, so its headers stay as they are.
        self.assertEqual(files["daily-metrics-template.csv"].decode("utf-8-sig").strip(),
                         "date,ad_id,spend,impressions,clicks,purchases,purchase_value")
        self.assertEqual(json.loads(files["manifest.json"])["language"], "zh-CN")


class TestSameRoundBothLanguages(ExportLanguageFixtures):
    def zh_package_of(self, row_id):
        """Export the same round as a zh-CN requester, without keeping it: one round has one package."""
        captured = {}
        zh = replace(self.context, ui_language="zh-CN")
        try:
            with tenant_session(zh) as session:
                record = workflow.get_round(session, zh, row_id)
                assets = [v.id for v, _, _ in workflow.creative_assets(session, zh, record)]
                workflow.create_handoff(session, zh, record, CreateHandoff(asset_version_ids=assets))
                handoff = workflow.scoped(session, zh, MarketingHandoffRecord, round_id=row_id)[0]
                captured["manifest"] = copy.deepcopy(handoff.manifest_json)
                raise _Rollback
        except _Rollback:
            pass
        return captured["manifest"], asyncio.run(package_bytes(captured["manifest"], self.blobs))

    def test_both_languages_share_entry_ids_ad_names_and_files(self):
        _, row = self.approved_round()
        zh_manifest, zh_zip = self.zh_package_of(row["id"])
        row = self.export(row)  # the real one, in English
        en_files, zh_files = self.files(self.package(row)), self.files(zh_zip)

        self.assertEqual(sorted(en_files), sorted(zh_files))
        for name in en_files:
            if name.startswith("media/") or name == "daily-metrics-template.csv":
                self.assertEqual(en_files[name], zh_files[name], name)
        en_entries = json.loads(en_files["manifest.json"])["entries"]
        zh_entries = json.loads(zh_files["manifest.json"])["entries"]
        stable = lambda entries: [(e["entry_id"], e["asset_version_id"], e["ad_name"], e["file"], e["content_sha256"])
                                  for e in entries]
        self.assertEqual(stable(en_entries), stable(zh_entries))
        self.assertEqual([r[:2] + r[3:] for r in self.ads(en_files)[1:]], [r[:2] + r[3:] for r in self.ads(zh_files)[1:]],
                         "every ad-list value except the display name is the same")
        self.assertNotEqual([e["display_name"] for e in en_entries], [e["display_name"] for e in zh_entries])
        self.assertNotEqual(en_files["README.txt"], zh_files["README.txt"])
        self.assertEqual(zh_manifest["language"], "zh-CN")


class TestOlderClients(ExportLanguageFixtures):
    def test_without_the_flag_the_round_keeps_its_pre_255_shape(self):
        project, row = self.approved_round()
        row = self.export(row)
        workflow_path = f"/api/v1/marketing/projects/{project['id']}/workflow"
        for legacy in (self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"],
                       self.client.get(workflow_path).json()["current"]):
            self.assertNotIn("language", legacy["handoff"])
            self.assertTrue(all("display_name" not in e for e in legacy["handoff"]["entries"]))
            self.assertEqual([e["ad_name"] for e in legacy["handoff"]["entries"]], [e["ad_name"] for e in row["handoff"]["entries"]])
        flagged = self.client.get(workflow_path, params={"include_text_language": "true"}).json()["current"]
        self.assertEqual(flagged["handoff"]["language"], "en")


class TestFrozenLanguage(ExportLanguageFixtures):
    def test_a_retry_or_worker_rebuilds_in_the_frozen_language(self):
        _, row = self.approved_round()
        self.speak("zh-CN")
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]}, 202)
        handoff_id = row["handoff"]["id"]
        with tenant_session(self.context) as session:
            frozen = copy.deepcopy(session.get(MarketingHandoffRecord, handoff_id).manifest_json)
        self.speak("en")  # someone else retries, and the Worker's own context is English
        asyncio.run(execute_export(self.context.for_system_execution(), handoff_id, self.blobs))
        row = self.read_round(row["id"])
        self.assertEqual(row["handoff"]["language"], "zh-CN")
        self.assertIn("保留每个广告名称里的 MA 编号", self.files(self.package(row))["README.txt"].decode())
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingHandoffRecord, handoff_id).manifest_json, frozen, "the manifest is never rewritten")

    def test_a_package_from_before_the_language_field_keeps_its_names_and_english_notes(self):
        _, row = self.approved_round()
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]}, 202)
        legacy_name = "MA-0123456789ab 清楚理解产品价值 / 使用情境演示 #1"
        with tenant_session(self.context) as session:
            record = session.get(MarketingHandoffRecord, row["handoff"]["id"])
            manifest = copy.deepcopy(record.manifest_json)
            manifest["schema_version"] = 1
            del manifest["language"]
            for entry in manifest["entries"]:
                del entry["display_name"]
                entry["ad_name"] = legacy_name
            record.manifest_json = manifest  # as stored before #255
        self.speak("zh-CN")
        asyncio.run(execute_export(self.context, row["handoff"]["id"], self.blobs))
        row = self.read_round(row["id"])
        self.assertIsNone(row["handoff"]["language"])
        self.assertEqual({e["ad_name"] for e in row["handoff"]["entries"]}, {legacy_name}, "history is not renamed")
        files = self.files(self.package(row))
        self.assertIn("Keep the MA key in each ad name", files["README.txt"].decode())
        self.assertEqual({r[1] for r in self.ads(files)[1:]}, {legacy_name})
        self.assertEqual({r[2] for r in self.ads(files)[1:]}, {legacy_name}, "no display name: the ad name stands in")


class TestExportTenancy(ExportLanguageFixtures):
    def test_another_workspace_cannot_export_or_read_the_round_in_any_language(self):
        _, row = self.approved_round()
        owner = self.context
        self.context = replace(self._make_context(), ui_language="zh-CN")
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/handoff", headers=self.headers(row["revision"]),
            json={"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["code"], "round_not_found")
        self.assertEqual(self.client.get(f"/api/v1/marketing/rounds/{row['id']}").status_code, 404)
        self.context = owner
        self.assertIsNone(self.read_round(row["id"])["handoff"])

    def test_a_member_exports_in_their_own_language_and_the_owner_reads_it_unchanged(self):
        _, row = self.approved_round()
        owner = self.context
        self.context = replace(self.owner_ctx(owner, actor="member_b"), role="member", ui_language="zh-CN")
        row = self.export(row)
        self.assertEqual(row["handoff"]["language"], "zh-CN")
        self.context = replace(owner, ui_language="en")
        seen = self.read_round(row["id"])["handoff"]
        self.assertEqual((seen["language"], seen["entries"]), ("zh-CN", row["handoff"]["entries"]),
                         "reading in another language never re-renders a frozen package")
