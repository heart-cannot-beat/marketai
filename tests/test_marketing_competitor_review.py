"""Issue #273 (C-24): plan drafting cites a competitor snapshot; plan review shows the three checks.

Real HTTP routes and the real Worker over the Controlled Mock P0 recordings
(the scan) and the Controlled Mock planner (the draft). A paid draft is
answered by a fake completion, so no real key and no real request is used.

Covered: spec scenario 8's drafting half (the latest snapshot of the same
product and market, ``market="北美"`` -> US, an unmatched market cites none and
the projection says so, ``competitor_snapshot_stale`` only for a newer snapshot
of that product and market with another hash), scenario 9 (crowded and gap
advisories that never block confirmation, reused competitor copy that does,
with its source observation, while the product's own page copy does not),
scenario 15 (an en-US plan citing a Chinese snapshot stays English), the
product switch and no snapshot drafting exactly as before, a client that
cannot point a plan at another snapshot, and the legacy projection for
clients that predate competitor review.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from tests.test_competitor_snapshots import SnapshotFixtures
from tests.test_competitors_api import BRIGHTVOLT, ACME
from tests.test_marketing_languages import PROJECT_READ, WORKFLOW_READ

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, utcnow
from app.competitors.models import CompetitorScanSnapshotRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import CredentialProtector
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.models import EntitlementProjectionRecord
from app.marketing import competitor_review
from app.marketing.models import MarketingProjectRecord, PlanningCallRecord
from app.marketing.planner import example_result, execute_planning
from app.marketing.workflow_models import MarketingRoundRecord

QUERY = ("include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true"
         "&include_competitor_review=true")
LEGACY = "include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true"
SOURCE = "Brightvolt NACS Adapter\nLets CCS1 electric cars charge at Halcyon Superchargers. UL 2252 certified."
ALIASES = {"北美": "US", "north america": "US", "美国": "US"}
PAID = {"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test", "MARKETAI_PLANNING_MAX_COST_USD": "0.10"}
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
LIFTED_HEADLINE = "Tired of Compatibility Headaches? Not Anymore."  # a relevant Acme Charge title in the P0 recordings
LIFTED_BODY = "We offer reliable and convenient solutions for all your charging needs."  # 9 words of a Acme Charge body
OWN_HEADLINE = "Enjoy Charging Without Limits."  # the product's own page (Brightvolt, role=self)


class CompetitorPlanFixtures(SnapshotFixtures):
    def setUp(self) -> None:
        super().setUp()
        aliases = patch.object(config, "MARKET_COUNTRY_ALIASES_JSON", json.dumps(ALIASES, ensure_ascii=False))
        aliases.start()
        self.addCleanup(aliases.stop)

    # -- marketing requests ---------------------------------------------------

    def _marketing_headers(self, revision=None) -> dict:
        return self._headers(**({"If-Match": f'"{revision}"'} if revision else {}))

    def _read(self, project: dict, query: str = QUERY) -> dict:
        response = self.client.get(f"/api/v1/marketing/projects/{project['id']}?{query}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _edit(self, project: dict, command: str, query: str = QUERY, **fields):
        return self.client.patch(f"/api/v1/marketing/projects/{project['id']}?{query}",
                                 headers=self._marketing_headers(project["revision"]), json={"command": command, **fields})

    def _saved(self, project: dict, plan: dict, command: str = "save_plan") -> dict:
        response = self._edit(project, command, plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _planning(self, project: dict, kind: str, mode: str = "mock", quote_hash: str = "",
                  strategy_language: str = "en-US") -> dict:
        response = self.client.post(
            f"/api/v1/marketing/projects/{project['id']}/planning", headers=self._marketing_headers(project["revision"]),
            json={"kind": kind, "execution_mode": mode, "quote_hash": quote_hash,
                  **({"ad_language": "en-US", "strategy_language": strategy_language} if kind == "plan" else {})})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def _understood(self) -> dict:
        response = self.client.post(f"/api/v1/marketing/projects?{QUERY}", headers=self._marketing_headers(),
                                    json={"source_text": SOURCE})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self._planning(project, "understanding")
        self._drain()
        project = self._read(project)
        understanding = project["understanding"]
        for fact in understanding["facts"]:
            fact["confirmation"] = "confirmed"
        response = self._edit(project, "confirm_understanding", understanding=understanding)
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()
        self._competitor_product = project["product_id"]  # competitors and scans belong to this product
        return project

    def _drafted(self, project: dict, **kwargs) -> dict:
        self._planning(project, "plan", **kwargs)
        self._drain()
        project = self._read(project)
        self.assertEqual(project["latest_call"]["state"], "succeeded", project["latest_call"])
        return project

    def _call(self, project: dict) -> PlanningCallRecord:
        with tenant_session(self.ctx) as session:
            call = session.get(PlanningCallRecord, project["latest_call"]["id"])
            session.expunge(call)
            return call

    def _stored(self, project: dict) -> dict:
        with tenant_session(self.ctx) as session:
            return deepcopy(session.get(MarketingProjectRecord, project["id"]).plan_json)

    def _ended_round(self, project: dict) -> dict:
        """A Controlled Mock round frozen from the project's plan, ended without delivery (#367)."""
        frozen = self.client.post(f"/api/v1/marketing/projects/{project['id']}/rounds",
            headers=self._marketing_headers(project["revision"]), json={
                "start_date": "2025-01-01", "end_date": "2025-01-07", "account_timezone": "UTC", "execution_mode": "mock",
                "protocol": {"min_days": 7, "min_impressions": 1000, "min_clicks": 20, "min_purchases": 20,
                             "min_spend": "20"}})
        self.assertEqual(frozen.status_code, 201, frozen.text)
        row = frozen.json()["data"]
        closed = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/close",
                                  headers=self._marketing_headers(row["revision"]),
                                  json={"reason": "insufficient_evidence", "note": "Controlled Mock only"})
        self.assertEqual(closed.status_code, 200, closed.text)
        return closed.json()["data"]

    def _workflow(self, project: dict, query: str = WORKFLOW_READ) -> dict:
        response = self.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?{query}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _frozen_snapshot(self, row: dict) -> dict:
        with tenant_session(self.ctx) as session:
            return deepcopy(session.get(MarketingRoundRecord, row["id"]).snapshot_json)

    def assert_reads_like_its_round(self, project: dict, row: dict) -> None:
        """#367: the page's project read and its round read differ only in the competitor review the round read
        never shows (#273); the page drops it from its side as the round read does (``asFrozen``)."""
        plan = self._read(project, PROJECT_READ)["plan"]
        current = self._workflow(project)["current"]
        self.assertEqual((current["id"], current["state"]), (row["id"], "closed"))
        frozen = current["snapshot"]["plan"]
        self.assertTrue(plan["competitor_snapshot_id"])
        self.assertTrue(any(angle["competitor_overlap"] for angle in plan["angles"]))
        self.assertTrue({"competitor_crowded", "competitor_gap"} <= {item["kind"] for item in plan["plan_review"]})
        self.assertNotIn("competitor_snapshot_id", frozen)
        self.assertFalse(any("competitor_overlap" in angle for angle in frozen["angles"]))
        self.assertFalse(any(item["kind"] in competitor_review.COMPETITOR_KINDS or "observations" in item
                             for item in frozen["plan_review"]))
        # The page compares the serialized plans: key for key, in order.
        self.assertNotEqual(json.dumps(plan), json.dumps(frozen))
        self.assertEqual(json.dumps(competitor_review.legacy_projection(plan)), json.dumps(frozen))
        # Only the read hides it: the stored round still cites the snapshot and keeps its findings.
        stored = self._frozen_snapshot(row)["plan"]
        self.assertEqual(stored["competitor_snapshot_id"], plan["competitor_snapshot_id"])
        self.assertEqual([item["kind"] for item in stored["plan_review"]], [item["kind"] for item in plan["plan_review"]])

    # -- competitor state -------------------------------------------------------

    def _us_scan(self, **body) -> str:
        """Acme Charge, Brightvolt as the product's own page and both keywords, in the US."""
        accepted = self._acme_and_self(**body)
        self._drain()
        return accepted["scan_id"]

    def _snapshot_id(self, scan_id: str) -> str:
        return self._record(scan_id).snapshot_id

    def _content(self, snapshot_id: str) -> dict:
        with tenant_session(self.ctx) as session:
            return deepcopy(session.get(CompetitorScanSnapshotRecord, snapshot_id).summary_json)

    @staticmethod
    def _findings(project: dict, kind: str) -> list[dict]:
        return [item for item in project["plan"]["plan_review"] if item["kind"] == kind]

    def _paid_setup(self) -> None:
        owner = self.owner_ctx(self.ctx)
        with tenant_session(owner) as session:
            admin = CredentialAdmin(CredentialProtector.local_only())
            credential = admin.create(session, owner, credential_key="planning", provider="openai",
                                      plaintext=b"tenant-planner-secret")
            admin.bind(session, owner, binding_scope="generation", provider="openai",
                       credential_version_id=credential.credential_version_id)
            session.add(EntitlementProjectionRecord(workspace_id=self.ctx.workspace_id, entitlement_version=1,
                lease_sequence=1, semantic_hash="planning-test", issued_at=utcnow(), valid_until=utcnow() + timedelta(days=1),
                resolved_entitlements_json={"controlled_mock": True}))
            session.add(SpendGuardrailRecord(workspace_id=self.ctx.workspace_id, budget_currency="USD",
                per_operation_limit=Decimal("1"), rolling_24h_limit=Decimal("5"), period_limit=Decimal("5"),
                outstanding_exposure_limit=Decimal("5"), created_at=utcnow(), updated_at=utcnow(), updated_by="owner"))

    def _paid_draft(self, project: dict, output, *, strategy_language="en-US") -> tuple[dict, AsyncMock]:
        """A byok draft answered by a fake completion: ``output(inputs) -> plan document``."""
        with patch.dict("os.environ", PAID):
            quote = self.client.get("/api/v1/marketing/capabilities").json()["paid"]["quote_hash"]
            call = self._planning(project, "plan", "byok", quote, strategy_language=strategy_language)
        with tenant_session(self.ctx) as session:
            row = session.get(PlanningCallRecord, call["id"])
            session.get(JobRecord, row.job_id).state = "running"
            inputs = deepcopy(row.input_json)
        complete = AsyncMock(return_value=json.dumps(output(inputs), ensure_ascii=False))
        asyncio.run(execute_planning(self.current, call["id"], complete=complete))
        project = self._read(project)
        self.assertEqual(project["latest_call"]["state"], "succeeded", project["latest_call"])
        return project, complete


class DraftingTests(CompetitorPlanFixtures):
    def test_a_draft_reads_the_us_snapshot_and_its_review_advises_without_blocking(self):
        project = self._understood()
        scan_id = self._us_scan()
        snapshot_id = self._snapshot_id(scan_id)
        frozen = self._content(snapshot_id)
        drafted = self._drafted(project)

        # What the planner read: the snapshot's clusters and counts, no media and no ad copy.
        read = self._call(drafted).input_json["competitor_snapshot"]
        self.assertEqual((read["snapshot_id"], read["country"]), (snapshot_id, "US"))
        self.assertEqual([c["cluster_id"] for c in read["claim_clusters"]],
                         [c["cluster_id"] for c in frozen["claim_clusters"]])
        self.assertTrue(all(len(c["claim"]) <= 200 for c in read["claim_clusters"]))
        text = json.dumps(read, ensure_ascii=False)
        for entry in frozen["ads"].values():
            for copy in entry["titles"] + entry["bodies"]:
                self.assertNotIn(copy, text)

        plan = drafted["plan"]
        self.assertEqual(plan["competitor_snapshot_id"], snapshot_id)
        self.assertEqual(plan["market"], "US")
        crowded_id = max(frozen["claim_clusters"], key=lambda c: (c["advertiser_count"], -int(c["cluster_id"][1:])))["cluster_id"]
        self.assertEqual([a["competitor_overlap"] for a in plan["angles"]], [[crowded_id], []])
        self.assertIn(f"(Competitor observation: {crowded_id})", plan["angles"][0]["rationale"])

        [crowded] = self._findings(drafted, "competitor_crowded")
        cluster = next(c for c in frozen["claim_clusters"] if c["cluster_id"] == crowded_id)
        self.assertEqual((crowded["severity"], crowded["source"], crowded["target_ids"]), ("advisory", "system", ["a1"]))
        self.assertIn(f"{cluster['advertiser_count']} advertisers other than your own page", crowded["finding"])
        self.assertTrue(crowded["observations"])
        for observation in crowded["observations"]:
            entry = frozen["ads"][observation["ad_id"]]
            self.assertNotEqual(entry["role"], "self")
            self.assertEqual((observation["cluster_id"], observation["library_url"]), (crowded_id, entry["library_url"]))
        [gap] = self._findings(drafted, "competitor_gap")
        self.assertEqual((gap["severity"], gap["target_ids"]), ("advisory", ["a2"]))
        self.assertNotIn("observations", gap)  # a finding with none stores and reads without the key
        self.assertIn("This may be white space, or others may have tried it without success.", gap["finding"])
        self.assertEqual(self._findings(drafted, "competitor_copy_reuse"), [])
        # The planner's own angle_overlap / evidence_gap items stay theirs; the competitor ones come last.
        kinds = [item["kind"] for item in plan["plan_review"]]
        self.assertEqual(kinds[-2:], ["competitor_crowded", "competitor_gap"])
        self.assertIn("evidence_gap", kinds[:-2])

        self.assertEqual((drafted["competitor_review_support"], drafted["competitor_snapshot_stale"],
                          drafted["competitor_market_unmatched"]), ("v1", False, False))
        # Advisories never block: the plan confirms as it is.
        confirmed = self._saved(drafted, {**plan, "executions_per_combination": 1}, "confirm_plan")
        self.assertEqual(confirmed["state"], "plan_confirmed")
        self.assertEqual(confirmed["plan"]["competitor_snapshot_id"], snapshot_id)
        self.assertEqual(len(self._findings(confirmed, "competitor_crowded")), 1)

    def test_a_client_predating_competitor_review_reads_and_writes_the_old_shape(self):
        project = self._understood()
        self._us_scan()
        drafted = self._drafted(project)
        legacy = self._read(drafted, LEGACY)
        self.assertNotIn("competitor", json.dumps(legacy))
        stored = self._stored(drafted)
        # Saving the old shape keeps the server's citation and the planner's overlaps.
        saved = self._edit(legacy, "save_plan", LEGACY, plan=legacy["plan"])
        self.assertEqual(saved.status_code, 200, saved.text)
        after = self._stored(drafted)
        self.assertEqual(after["competitor_snapshot_id"], stored["competitor_snapshot_id"])
        self.assertEqual([a["competitor_overlap"] for a in after["angles"]],
                         [a["competitor_overlap"] for a in stored["angles"]])
        self.assertNotIn("competitor", json.dumps(saved.json()))


class RoundReadTests(CompetitorPlanFixtures):
    def test_a_cited_plan_and_its_ended_round_differ_only_in_the_competitor_review(self):
        project = self._understood()
        self._us_scan()
        drafted = self._drafted(project)
        confirmed = self._saved(drafted, {**drafted["plan"], "executions_per_combination": 1}, "confirm_plan")
        row = self._ended_round(confirmed)
        snapshot = self._frozen_snapshot(row)
        self.assert_reads_like_its_round(confirmed, row)
        self.assertEqual(self._frozen_snapshot(row), snapshot)


class CopyReuseTests(CompetitorPlanFixtures):
    def test_scenario_9_reused_copy_blocks_with_its_source_and_own_copy_does_not(self):
        project = self._understood()
        snapshot_id = self._snapshot_id(self._us_scan())
        frozen = self._content(snapshot_id)
        drafted = self._drafted(project)

        plan = deepcopy(drafted["plan"])
        plan["combinations"][0]["headline"] = LIFTED_HEADLINE
        plan["combinations"][1]["body_copy"] = LIFTED_BODY
        saved = self._saved(drafted, plan)
        reused = self._findings(saved, "competitor_copy_reuse")
        self.assertEqual([(r["severity"], r["source"], r["target_ids"]) for r in reused],
                         [("blocking", "system", [plan["combinations"][0]["id"]]),
                          ("blocking", "system", [plan["combinations"][1]["id"]])])
        self.assertIn("The headline of the combination", reused[0]["finding"])
        self.assertIn("The body copy of the combination", reused[1]["finding"])
        for item, copy in ((reused[0], LIFTED_HEADLINE), (reused[1], "reliable and convenient solutions for all your charging needs")):
            self.assertTrue(item["observations"])
            for observation in item["observations"]:
                entry = frozen["ads"][observation["ad_id"]]
                self.assertEqual((entry["advertiser_name"], entry["role"]), ("Acme Charge", "competitor"))
                self.assertEqual(observation["library_url"], entry["library_url"])
                self.assertTrue(any(copy.casefold().rstrip(".") in text.casefold()
                                    for text in entry["titles"] + entry["bodies"]))

        blocked = self._edit(saved, "confirm_plan", plan={**saved["plan"], "executions_per_combination": 1})
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()["code"], "plan_review_unresolved")
        index = {item["id"]: i for i, item in enumerate(saved["plan"]["plan_review"])}
        self.assertEqual([e["field"] for e in blocked.json()["field_errors"]],
                         [f"body.plan.plan_review.{index[item['id']]}" for item in reused])
        self.assertEqual(self._read(saved)["revision"], saved["revision"])

        # The same whole title on the product's own page is the user's to reuse; rewritten copy clears the block.
        fixed = deepcopy(saved["plan"])
        fixed["combinations"][0]["headline"] = OWN_HEADLINE
        fixed["combinations"][1]["body_copy"] = drafted["plan"]["combinations"][1]["body_copy"]
        confirmed = self._saved(saved, {**fixed, "executions_per_combination": 1}, "confirm_plan")
        self.assertEqual(self._findings(confirmed, "competitor_copy_reuse"), [])
        self.assertEqual(confirmed["state"], "plan_confirmed")

    def test_an_english_plan_that_does_not_copy_is_not_flagged_by_the_p0_acme_copy(self):
        project = self._understood()
        self._us_scan()
        drafted = self._drafted(project)
        plan = deepcopy(drafted["plan"])
        copy = [("One adapter for CCS1 cars at Halcyon Motors stations", "Plug a CCS1 car into the Halcyon Motors network with a compact adapter rated to UL 2252."),
                ("Road trips with more places to charge", "Find more charging stops on long drives; check your car's compatibility list first."),
                ("Charge safely in rain or snow", "A sealed housing and a firm latch keep the connection steady through winter weather."),
                ("Know what fits before you buy", "Our guide lists the models and chargers this adapter supports, and those it does not."),
                ("Fewer detours, more charging", "Use the fast chargers already on your route instead of planning around one network.")]
        for combo, (headline, body) in zip(plan["combinations"], copy):
            combo.update(headline=headline, body_copy=body)
        saved = self._saved(drafted, plan)
        self.assertEqual(self._findings(saved, "competitor_copy_reuse"), [])


class MarketTests(CompetitorPlanFixtures):
    def test_scenario_8_market_aliases_unmatched_markets_and_staleness(self):
        project = self._understood()
        us_scan = self._us_scan()
        us_snapshot = self._snapshot_id(us_scan)
        drafted = self._drafted(project)
        self.assertEqual(drafted["plan"]["competitor_snapshot_id"], us_snapshot)

        # "北美" names the US: the citation stays.
        north_america = self._saved(drafted, {**drafted["plan"], "market": "北美"})
        self.assertEqual(north_america["plan"]["competitor_snapshot_id"], us_snapshot)
        self.assertFalse(north_america["competitor_market_unmatched"])

        # A new scan in another market is not newer for this plan.
        self._us_scan(country="GB")
        self.assertFalse(self._read(north_america)["competitor_snapshot_stale"])
        # An unchanged refreeze returns the same snapshot: still not stale.
        self.assertEqual(self._refreeze(us_scan)["snapshot_id"], us_snapshot)
        self.assertFalse(self._read(north_america)["competitor_snapshot_stale"])
        # A flip changes the hash: the plan keeps the snapshot it read, and it is now stale.
        ad = next(ad for ad in self._all_ads(us_scan) if ad["relevance"]["relevance"] == "relevant")
        self.assertEqual(self._flip(us_scan, ad["ad_id"], "unrelated").status_code, 200)
        newer = self._refreeze(us_scan)["snapshot_id"]
        self.assertNotEqual(newer, us_snapshot)
        stale = self._read(north_america)
        self.assertTrue(stale["competitor_snapshot_stale"])
        self.assertEqual(stale["plan"]["competitor_snapshot_id"], us_snapshot)

        # "火星" names no country: no citation, no competitor finding, and the projection says why.
        mars = self._saved(stale, {**stale["plan"], "market": "火星"})
        self.assertNotIn("competitor_snapshot_id", mars["plan"])
        self.assertFalse(any(item["kind"].startswith("competitor_") for item in mars["plan"]["plan_review"]))
        self.assertTrue(mars["competitor_market_unmatched"])
        self.assertFalse(mars["competitor_snapshot_stale"])
        # A redraft keeps that market and reads no snapshot.
        redrafted = self._drafted(mars)
        self.assertNotIn("competitor_snapshot", self._call(redrafted).input_json)
        self.assertNotIn("competitor_snapshot_id", redrafted["plan"])

    def test_a_paid_draft_maps_its_market_and_an_english_plan_stays_english(self):
        self._paid_setup()
        project = self._understood()
        self.current = dataclasses.replace(self.ctx, ui_language="zh-CN")  # the scan reasons in Chinese
        snapshot_id = self._snapshot_id(self._us_scan())
        self.current = self.ctx
        frozen = self._content(snapshot_id)
        self.assertEqual(frozen["language"], "zh-CN")
        first, second = frozen["claim_clusters"][0]["cluster_id"], frozen["claim_clusters"][1]["cluster_id"]

        def output(inputs):
            self.assertEqual(inputs["competitor_snapshot"]["language"], "zh-CN")
            document = example_result("plan", {**inputs, "competitor_snapshot": None})
            document["market"] = "北美"
            document["angles"][0]["competitor_overlap"] = [first, second, "c99"]
            document["angles"][1]["competitor_overlap"] = []
            document["plan_review"].append({"id": "r9", "kind": "competitor_gap", "severity": "advisory", "target_ids": [],
                                            "finding": "Forged by the model.", "suggestion": "None.", "source": "ai"})
            return document

        drafted, complete = self._paid_draft(project, output)
        system, user = complete.await_args.args[3], complete.await_args.args[4]
        self.assertIn("用en-US转述竞品主张", system)
        self.assertIn("「Competitor observation」", system)
        self.assertIn('"competitor_overlap"', system)
        self.assertIn(f'"snapshot_id": "{snapshot_id}"', user)
        plan = drafted["plan"]
        self.assertEqual((plan["market"], plan["competitor_snapshot_id"]), ("北美", snapshot_id))
        self.assertEqual(plan["angles"][0]["competitor_overlap"], [first, second])  # c99 is not in the snapshot
        self.assertTrue(plan["angles"][0]["rationale"].endswith(f" (Competitor observation: {first}, {second})"))
        self.assertNotIn("Forged by the model.", json.dumps(plan))
        # Scenario 15: the strategy text and every competitor finding are English.
        competitor = [item for item in plan["plan_review"] if item["kind"].startswith("competitor_")]
        self.assertEqual({item["kind"] for item in competitor}, {"competitor_crowded", "competitor_gap"})
        for item in competitor:
            self.assertIsNone(CJK.search(item["finding"] + item["suggestion"]), item["finding"])
        for angle in plan["angles"]:
            self.assertIsNone(CJK.search(angle["proposition"] + angle["rationale"]))


class SwitchTests(CompetitorPlanFixtures):
    def test_switched_off_or_without_a_snapshot_a_draft_is_exactly_as_before(self):
        project = self._understood()
        without = self._drafted(project)
        call = self._call(without)
        self.assertNotIn("competitor_snapshot", call.input_json)
        self.assertNotIn("competitor", json.dumps(self._stored(without)))
        self.assertNotIn("competitor", json.dumps(call.output_json))
        self.assertNotIn("competitor_snapshot_id", without["plan"])
        self.assertFalse(without["competitor_snapshot_stale"])
        baseline = self._stored(without)

        self._us_scan()
        self.assertEqual(self._switch(False).status_code, 200)
        off = self._drafted(without)
        self.assertNotIn("competitor_snapshot", self._call(off).input_json)
        self.assertEqual(self._stored(off), baseline)

        self.assertEqual(self._switch(True).status_code, 200)
        on = self._drafted(off)
        self.assertIn("competitor_snapshot", self._call(on).input_json)
        self.assertIn("competitor_snapshot_id", on["plan"])

    def test_a_client_cannot_point_a_plan_at_another_snapshot(self):
        project = self._understood()
        snapshot_id = self._snapshot_id(self._us_scan())
        drafted = self._drafted(project)
        # Another product's snapshot in the same Workspace.
        own_product = self._competitor_product
        self._competitor_product = None
        other = self._snapshot_id(self._us_scan())
        self._competitor_product = own_product
        for forged in (other, "csnap_does_not_exist"):
            saved = self._saved(drafted, {**drafted["plan"], "competitor_snapshot_id": forged})
            self.assertEqual(saved["plan"]["competitor_snapshot_id"], snapshot_id)
            drafted = saved
        # Another Workspace cannot read the plan at all.
        self.current = self._make_context()
        response = self.client.get(f"/api/v1/marketing/projects/{project['id']}?{QUERY}")
        self.assertEqual(response.status_code, 404, response.text)
        self.current = self.ctx
        foreign = self._make_context()
        with tenant_session(foreign) as session:
            self.assertIsNone(competitor_review.snapshot(session, foreign, snapshot_id, own_product))
