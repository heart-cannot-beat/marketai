"""Issue #272 (C-23) -- tags, buying-claim clusters, reclustering and snapshots.

Real HTTP routes (``TestClient``) and the real Worker (``WorkerRuntime``)
over the Controlled Mock P0 recordings. "byok" scans are answered by a fake
OpenAI that answers like the Mock: no real key, no real request, but the
``tag:<i>`` / ``cluster:1`` reservations, the attempts, the settlement and
the releases run for real. The product's dimension vocabulary is patched to
a small fixed one (building it from test plans is the external-creative
tests' job, Issue #168); the scan freezes whatever it returns.

Covered: the tag table (relevant observations longest-running first, at most
``max_tagged_ads``, ``tag:<i>`` names, unused ordinals released when the
step ends), vision requests within the tag reservation, redelivered tag and
cluster calls replayed without new attempts; the Mock clusters (Northpeak's
safety anxiety, Acme Charge's one-adapter-for-every-charger, a cluster with the
product's own page); spec scenarios 7 (second half), 8, 14 and 15; freeze
determinism (no new row, no LLM call), flips moving observations out of
clusters or into "unclustered"; a paid recluster with its quote, its own
Job's ``cluster:1`` reservation and settlement; tag confirmations; resumes
after the AI key is refused mid-filter or mid-tag; the product switch; and
another Workspace refused with 404.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
from decimal import Decimal
from unittest.mock import patch

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import DatabaseError

from tests.test_competitor_relevance import NORTHPEAK, RelevanceFixtures
from tests.test_competitor_scans_api import HALCYON, Killed
from tests.test_competitors_api import BRIGHTVOLT, ACME

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, ProductRecord
from app.competitors import clusters, scan_plan, snapshots, tags
from app.competitors.models import (
    CompetitorScanAssessmentRecord,
    CompetitorScanRecord,
    CompetitorScanSnapshotRecord,
)
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
from app.providers.llm_chat import llm_call_key

VOCABULARY = {
    "version": "external-tagging-vocabulary/v1", "directions": [], "plans": [],
    "factors": [
        {"factor_id": "hook", "label": "Hook", "source": "test_plan", "values": [
            {"value_id": "v1", "label": "Safety fear", "members": []},
            {"value_id": "v2", "label": "Compatibility", "members": []},
            {"value_id": "v3", "label": "Price", "members": []},
        ]},
        {"factor_id": "visual", "label": "Visual", "source": "test_plan", "values": [
            {"value_id": "v1", "label": "Product close-up", "members": []},
            {"value_id": "v2", "label": "In use", "members": []},
        ]},
    ],
}
REJECTED = httpx.Response(401, json={"error": {"message": "bad key", "code": "invalid_api_key"}})
EVERY_CHARGER = "One adapter set covers every charger"
SAFETY = "Charging safety anxiety"


class SnapshotFixtures(RelevanceFixtures):
    vocabulary: dict | None = VOCABULARY
    #: True leaves the product's real vocabulary in place (the expression catalog's factor, #329).
    real_vocabulary = False

    def setUp(self) -> None:
        super().setUp()
        if self.real_vocabulary:
            return
        vocabulary = self.vocabulary
        patcher = patch("app.external_creatives.vocabulary.build_vocabulary",
                        lambda *args, **kwargs: copy.deepcopy(vocabulary) if vocabulary else {
                            "version": "external-tagging-vocabulary/v1", "factors": [], "directions": [],
                            "plans": []})
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- requests ------------------------------------------------------------

    def _summary(self, scan_id: str, expect: int = 200, **params) -> dict:
        response = self.client.get(f"/api/v1/competitor-scans/{scan_id}/summary", params=params)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _refreeze(self, scan_id: str, *, key: str | None = None, expect: int = 200) -> dict:
        response = self.client.post(f"/api/v1/competitor-scans/{scan_id}/snapshots", headers=self._headers(key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _recluster_quote(self, scan_id: str, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/competitor-scans/{scan_id}/recluster-quote")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _recluster(self, scan_id: str, *, key: str | None = None, expect: int = 202) -> dict:
        response = self.client.post(f"/api/v1/competitor-scans/{scan_id}/recluster", headers=self._headers(key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _confirm_tags(self, scan_id: str, ad_id: str, values: list[dict], *, key: str | None = None):
        return self.client.post(f"/api/v1/competitor-scans/{scan_id}/ads/{ad_id}/tags",
                                json={"tags": values}, headers=self._headers(key))

    def _tags(self, scan_id: str, ad_id: str) -> dict:
        response = self.client.get(f"/api/v1/competitor-scans/{scan_id}/ads/{ad_id}/tags")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _settings(self, product_id: str | None = None):
        return self.client.get(f"/api/v1/products/{product_id or self.product_id}/competitor-settings")

    def _switch(self, value: bool, *, key: str | None = None, product_id: str | None = None):
        return self.client.put(f"/api/v1/products/{product_id or self.product_id}/competitor-settings",
                               json={"competitor_snapshot_in_drafts": value}, headers=self._headers(key))

    # -- state -----------------------------------------------------------------

    def _snapshot_rows(self, scan_id: str) -> list[CompetitorScanSnapshotRecord]:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(select(CompetitorScanSnapshotRecord).where(
                CompetitorScanSnapshotRecord.scan_id == scan_id)).all()
            for row in rows:
                session.expunge(row)
            return rows

    def _cluster_rows(self, scan_id: str) -> list[CompetitorScanAssessmentRecord]:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(select(CompetitorScanAssessmentRecord).where(
                CompetitorScanAssessmentRecord.scan_id == scan_id
            ).order_by(CompetitorScanAssessmentRecord.revision)).all()
            for row in rows:
                session.expunge(row)
            return rows

    def _workspace_llm_attempts(self) -> int:
        with tenant_session(self.ctx) as session:
            return session.scalar(select(func.count()).select_from(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.workspace_id == self.ctx.workspace_id,
                ProviderCallAttemptRecord.step_kind == "llm_chat"))

    def _attempt_names(self, job_id: str) -> dict[str, str]:
        """call name -> dispatch state of every ``llm_chat`` attempt of ``job_id``."""
        names = {llm_call_key(job_id, name): name for name in
                 [f"filter:{i}" for i in range(1, 100)] + [f"tag:{i}" for i in range(1, 201)] + ["cluster:1"]
                 + [f"discover:{i}" for i in range(1, 101)]}
        return {names[a.call_semantic_key_hash]: a.dispatch_state for a in self._llm_attempts(job_id)}

    def _open_reservations(self, *job_ids: str) -> list[str]:
        return [key for job_id in job_ids for key, row in self._scan_reservations(job_id).items()
                if row.state == "reserved"]

    def _steps(self, scan_id: str) -> dict[str, tuple]:
        return {step["step"]: (step["status"], step["reason"]) for step in self._scan(scan_id)["steps"]}

    def _acme_and_self(self, **body) -> dict:
        """Acme Charge and Brightvolt (as the product's own page) plus both keywords."""
        ids = [self._confirmed(ACME), self._confirmed(BRIGHTVOLT, role="self")]
        return self._start(self._body(ids, **body))


# ---------------------------------------------------------------------------
# The Worker's tag, cluster and freeze steps
# ---------------------------------------------------------------------------


class MockStepTests(SnapshotFixtures):
    def test_a_scan_tags_clusters_and_freezes_its_relevant_observations(self):
        accepted = self._acme_and_self()
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual((scan["status"], scan["stage"]), ("completed", "freeze"))
        self.assertEqual(self._steps(accepted["scan_id"]), {
            "fetch": ("completed", None), "media": ("completed", None), "filter": ("completed", None),
            "tag": ("completed", None), "cluster": ("completed", None), "freeze": ("completed", None),
        })
        ads = self._all_ads(accepted["scan_id"])
        relevant = [ad for ad in ads if ad["relevance"]["relevance"] == "relevant"]
        self.assertGreater(len(relevant), 60)

        # The frozen table: relevant, longest running first then ad_archive_id, at most max_tagged_ads.
        state = self._record(accepted["scan_id"]).evidence_json["tag"]
        ranked = sorted(relevant, key=lambda ad: (-(ad["days_running"] if ad["days_running"] is not None else -1),
                                                  ad["ad_archive_id"]))
        self.assertEqual([entry["ad_id"] for entry in state["assignments"].values()],
                         [ad["ad_id"] for ad in ranked[:60]])
        self.assertEqual(list(state["assignments"]), [f"tag:{i}" for i in range(1, 61)])
        self.assertEqual(state["answered"], list(state["assignments"]))
        self.assertEqual(state["vocabulary_hash"], tags.canonical_hash(VOCABULARY))
        tagged = {ad["ad_id"] for ad in ads if ad["tags"]}
        self.assertEqual(tagged, {ad["ad_id"] for ad in ranked[:60]})  # past the cap: untagged
        self.assertTrue(all(ad["tags"]["ai_suggestion"] and ad["tags"]["source"] == "ai"
                            for ad in ads if ad["tags"]))

        # Clusters: AI inference; members computed from the observations.
        summary = self._summary(accepted["scan_id"])
        self.assertEqual(summary["snapshot_id"], scan["snapshot_id"])
        self.assertTrue(summary["current"] and not summary["pending_changes"] and summary["mock"])
        claims = {cluster["claim"]: cluster for cluster in summary["clusters"]}
        every = next(c for claim, c in claims.items() if claim.startswith(EVERY_CHARGER))
        safety = next(c for claim, c in claims.items() if claim.startswith(SAFETY))
        self.assertIn("Acme Charge", {a["advertiser_name"] for a in every["advertisers"]})
        self.assertIn(NORTHPEAK, {a["advertiser_name"] for a in safety["advertisers"]})
        own = [c for c in summary["clusters"] if c["self_included"]]
        self.assertTrue(own)
        for cluster in summary["clusters"]:
            self.assertTrue(cluster["ai_inference"])
            self.assertEqual(cluster["advertiser_count"],
                             len([a for a in cluster["advertisers"] if a["role"] != "self"]))
            self.assertEqual(cluster["max_days_running"], max(ad["days_running"] for ad in cluster["ads"]))
        self.assertIn("Brightvolt", {a["advertiser_name"] for c in own for a in c["advertisers"] if a["role"] == "self"})
        self.assertTrue(summary["conclusion"]["ai_inference"])
        self.assertIn("Controlled Mock", summary["conclusion"]["text"])
        clustered = {ad_id for c in summary["clusters"] for ad_id in c["ad_ids"]}
        self.assertEqual({ad["ad_id"] for ad in summary["unclustered"]},
                         {ad["ad_id"] for ad in relevant} - clustered)

        # Scenario 14: the product's own page is listed apart and never counted as crowding.
        own_ads = {ad["ad_id"] for ad in ads if ad["role"] == "self"
                   and ad["relevance"]["relevance"] in ("relevant", "adjacent")}
        self.assertEqual({ad["ad_id"] for ad in summary["self_observations"]}, own_ads)
        self.assertEqual(summary["counts"]["self"], len(own_ads))
        # Adjacent and long-running sections; dimension counts are AI suggestions only so far.
        self.assertEqual({ad["ad_id"] for ad in summary["adjacent"]},
                         {ad["ad_id"] for ad in ads if ad["relevance"]["relevance"] == "adjacent"})
        self.assertTrue(all(ad["days_running"] >= 60 for ad in summary["long_running"]))
        hook = next(item for item in summary["dimension_counts"] if item["factor_id"] == "hook")
        self.assertEqual(sum(v["confirmed"] for v in hook["values"]), 0)
        self.assertEqual(sum(v["suggested"] for v in hook["values"]) + hook["none"]["suggested"], 60)
        self.assertEqual(hook["none"]["missing"], len(relevant) - 60)
        self.assertEqual(summary["relevance_sources"]["ai_inferred"], len(relevant))

    def test_scenario_15_a_zh_cn_scan_writes_claims_tags_and_the_conclusion_in_chinese(self):
        self.current = dataclasses.replace(self.ctx, ui_language="zh-CN")
        accepted = self._acme_and_self(keywords=[])
        self._drain()
        summary = self._summary(accepted["scan_id"])
        self.current = self.ctx
        self.assertEqual(summary["language"], "zh-CN")
        self.assertTrue(all("（受控 Mock）" in cluster["claim"] for cluster in summary["clusters"]))
        self.assertIn("一套转接头覆盖所有充电桩（受控 Mock）", {c["claim"] for c in summary["clusters"]})
        self.assertTrue(summary["conclusion"]["text"].startswith("受控 Mock"))
        self.assertEqual(summary["conclusion"]["language"], "zh-CN")
        ad = next(ad for ad in self._all_ads(accepted["scan_id"]) if ad["tags"])
        history = self._tags(accepted["scan_id"], ad["ad_id"])
        self.assertEqual(history["current"]["language"], "zh-CN")
        self.assertTrue(all(item["reason"].startswith("受控 Mock") for item in history["current"]["factors"]))
        # An English scan of the same product answers in English.
        english = self._start(self._body([self._confirmed(ACME)], keywords=[]))
        self._drain()
        claims = [cluster["claim"] for cluster in self._summary(english["scan_id"])["clusters"]]
        self.assertTrue(claims and all(claim.endswith("(Controlled Mock)") for claim in claims))

    def test_a_product_without_a_vocabulary_is_not_tagged_but_still_clustered(self):
        # Since #329 every new vocabulary has the expression catalog's factor; an empty one is a
        # scan frozen before it, which keeps its vocabulary (and resumes untagged).
        self.vocabulary = None
        with patch("app.external_creatives.vocabulary.build_vocabulary",
                   lambda *a, **k: {"version": "v", "factors": [], "directions": [], "plans": []}):
            accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
            self._drain()
        steps = self._steps(accepted["scan_id"])
        self.assertEqual(steps["tag"], ("skipped", "no_vocabulary"))
        self.assertEqual(steps["cluster"], ("completed", None))
        self.assertEqual(self._summary(accepted["scan_id"])["dimension_counts"], [])
        self._problem(self._confirm_tags(accepted["scan_id"], self._all_ads(accepted["scan_id"])[0]["ad_id"], []),
                      409, "competitor_tags_unavailable")

    def test_the_vocabulary_is_frozen_when_the_scan_is_admitted(self):
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        state = self._record(accepted["scan_id"]).evidence_json["tag"]  # before any Worker ran
        self.assertEqual((state["vocabulary"], state["vocabulary_hash"]), (VOCABULARY, tags.canonical_hash(VOCABULARY)))
        # A scan admitted by a release before #272 has none: the Worker skips tagging rather than build one.
        with tenant_session(self.ctx) as session:
            record = session.get(CompetitorScanRecord, accepted["scan_id"])
            record.evidence_json = {key: value for key, value in record.evidence_json.items() if key != "tag"}
        self._drain()
        self.assertEqual(self._steps(accepted["scan_id"])["tag"], ("skipped", "vocabulary_not_frozen"))
        self.assertEqual(self._steps(accepted["scan_id"])["cluster"], ("completed", None))

    def test_a_scan_without_observations_freezes_nothing(self):
        accepted = self._start(self._body([self._confirmed("999000999")], keywords=[]))
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual((scan["status"], scan["snapshot_id"]), ("completed", None))
        self.assertEqual(self._steps(accepted["scan_id"])["freeze"], ("skipped", "no_observations"))
        response = self.client.post(f"/api/v1/competitor-scans/{accepted['scan_id']}/snapshots",
                                    headers=self._headers())
        self._problem(response, 409, "competitor_scan_nothing_to_freeze")
        self._problem(self.client.get(f"/api/v1/competitor-scans/{accepted['scan_id']}/summary"),
                      404, "competitor_snapshot_not_found")


class ByokStepTests(SnapshotFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def test_tag_calls_are_named_by_the_table_and_the_rest_is_released(self):
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        job_id = accepted["job_id"]
        self._drain()
        state = self._record(accepted["scan_id"]).evidence_json["tag"]
        relevant = len(self._all_ads(accepted["scan_id"], relevance="relevant"))
        self.assertEqual(len(state["assignments"]), relevant)  # Brightvolt: fewer than max_tagged_ads
        names = [f"tag:{i}" for i in range(1, relevant + 1)]
        self.assertEqual(list(state["assignments"]), names)
        reservations = self._scan_reservations(job_id)
        tag_states = {name: reservations[llm_call_key(job_id, name)].state for name in
                      (f"tag:{i}" for i in range(1, 61))}
        self.assertEqual({name for name, value in tag_states.items() if value == "settled"}, set(names))
        unused = [f"tag:{i}" for i in range(relevant + 1, 61)]
        self.assertEqual({tag_states[name] for name in unused}, {"released"})
        self.assertEqual(state["released"], unused)  # released when the step ended, not at the scan's end
        self.assertEqual(reservations[llm_call_key(job_id, "cluster:1")].state, "settled")
        self.assertEqual(self._open_reservations(job_id), [])
        # One vision request per observation, within the tag reservation.
        tag_requests = [body for body in self.openai_requests
                        if body["response_format"]["json_schema"]["name"] == tags.SCHEMA_NAME]
        self.assertEqual(len(tag_requests), relevant)
        for body in tag_requests:
            self.assertEqual(body["max_completion_tokens"], 2_000)
            parts = body["messages"][-1]["content"]
            images = [part for part in parts if part["type"] == "image_url"]
            self.assertTrue(1 <= len(images) <= scan_plan.TAG_MAX_IMAGES)
        cluster_requests = [body for body in self.openai_requests
                            if body["response_format"]["json_schema"]["name"] == clusters.SCHEMA_NAME]
        self.assertEqual(len(cluster_requests), 1)
        self.assertEqual(cluster_requests[0]["max_completion_tokens"], 8_000)

    def test_a_dco_ad_is_tagged_card_by_card(self):
        accepted = self._start(self._body([self._confirmed(ACME)], keywords=[]))
        self._drain()
        def media_cards(ad) -> list[int]:
            return [card["index"] for card in ad["cards"]
                    if any((card[kind] or {}).get("status") == "available" for kind in ("image", "video_poster"))]

        dco = max((ad for ad in self._all_ads(accepted["scan_id"]) if ad["display_format"] == "DCO" and ad["tags"]),
                  key=lambda ad: len(media_cards(ad)))
        cards = media_cards(dco)
        self.assertGreaterEqual(len(cards), 2)  # several cards, each with its own image
        history = self._tags(accepted["scan_id"], dco["ad_id"])
        seen = [image["card_index"] for image in history["current"]["images"]]
        self.assertEqual(seen, cards[:scan_plan.TAG_MAX_IMAGES])  # card by card, in order
        self.assertEqual(len(history["items"]), 1)  # one synthesized suggestion for the whole ad

    def test_a_redelivered_job_replays_tag_and_cluster_calls_without_new_attempts(self):
        original = tags.normalize
        calls = []

        def die_after_the_first_tag(data, vocabulary):
            calls.append(1)
            if len(calls) == 1:
                raise Killed("worker killed after the answer, before the tags")
            return original(data, vocabulary)

        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        runtime = self._runtime()
        with patch.object(tags, "normalize", die_after_the_first_tag), self.assertRaises(Killed):
            for _ in range(5):
                asyncio.run(runtime.run_once())
        frozen = self._record(accepted["scan_id"]).evidence_json["tag"]
        self.assertEqual(frozen["answered"], [])
        sent = len(self.openai_requests)

        self._redeliver(accepted["job_id"])
        self._drain()
        state = self._record(accepted["scan_id"]).evidence_json["tag"]
        self.assertEqual(state["assignments"], frozen["assignments"])  # read, not recomputed
        self.assertTrue(state["calls"]["tag:1"]["replayed"])
        attempts = self._llm_attempts(accepted["job_id"])
        keys = [a.call_semantic_key_hash for a in attempts]
        self.assertEqual(len(keys), len(set(keys)))  # one attempt per call name, ever
        tag_sent = len([body for body in self.openai_requests[sent:]
                        if body["response_format"]["json_schema"]["name"] == tags.SCHEMA_NAME])
        self.assertEqual(tag_sent, len(state["assignments"]) - 1)  # tag:1 is not resent
        self.assertEqual(self._open_reservations(accepted["job_id"]), [])

        # A cluster answer lost the same way is replayed too.
        accepted = self._start(self._body([self._confirmed(ACME)], keywords=[]))
        runtime = self._runtime()
        original_clusters = clusters.claim_clusters
        with patch.object(clusters, "claim_clusters", side_effect=Killed("killed")), self.assertRaises(Killed):
            for _ in range(5):
                asyncio.run(runtime.run_once())
        sent = len(self.openai_requests)
        self._redeliver(accepted["job_id"])
        with patch.object(clusters, "claim_clusters", original_clusters):
            self._drain()
        self.assertEqual(len(self._cluster_rows(accepted["scan_id"])), 1)
        self.assertEqual([body for body in self.openai_requests[sent:]
                          if body["response_format"]["json_schema"]["name"] == clusters.SCHEMA_NAME], [])
        self.assertEqual(self._steps(accepted["scan_id"])["cluster"], ("completed", None))


# ---------------------------------------------------------------------------
# Freeze determinism and flips (spec §7.1, scenario 8)
# ---------------------------------------------------------------------------


class FreezeTests(SnapshotFixtures):
    def test_scenario_8_the_same_revisions_freeze_to_the_same_snapshot_without_llm(self):
        accepted = self._acme_and_self(keywords=[])
        self._drain()
        scan_id = accepted["scan_id"]
        first = self._scan(scan_id)["snapshot_id"]
        before = self._workspace_llm_attempts()
        again = self._refreeze(scan_id)
        once_more = self._refreeze(scan_id)
        self.assertEqual((again["snapshot_id"], again["created"]), (first, False))
        self.assertEqual((once_more["snapshot_id"], once_more["hash"]), (first, again["hash"]))
        self.assertEqual(len(self._snapshot_rows(scan_id)), 1)  # no new row
        self.assertEqual(self._workspace_llm_attempts(), before)  # freezing calls no LLM
        # The same key replays; the Owner may refreeze too.
        self.assertTrue(self._refreeze(scan_id, key="refreeze-1")["created"] is False)
        self.assertTrue(self._refreeze(scan_id, key="refreeze-1")["replayed"])
        self.current = self.owner_ctx(self.ctx)
        self.assertEqual(self._refreeze(scan_id)["snapshot_id"], first)
        self.current = self.ctx

        # Flip a clustered observation away: a new hash, and it leaves its cluster.
        summary = self._summary(scan_id)
        cluster = next(c for c in summary["clusters"] if c["observation_count"] > 1)
        member = cluster["ad_ids"][0]
        self.assertEqual(self._flip(scan_id, member, "unrelated").status_code, 200)
        self.assertTrue(self._summary(scan_id)["pending_changes"])
        flipped = self._refreeze(scan_id)
        self.assertTrue(flipped["created"])
        self.assertNotEqual(flipped["hash"], again["hash"])
        after = self._summary(scan_id)
        self.assertFalse(after["pending_changes"])
        self.assertNotIn(member, {ad_id for c in after["clusters"] for ad_id in c["ad_ids"]})
        self.assertNotIn(member, flipped["content"]["relevant_ad_ids"])
        self.assertEqual(self._workspace_llm_attempts(), before)

        # Flipping back is a third revision: another hash (the snapshot cites revision IDs), and the
        # observation is back in the cluster that still lists it.
        self._flip(scan_id, member, "relevant")
        back = self._refreeze(scan_id)
        self.assertTrue(back["created"])
        self.assertNotIn(back["hash"], (again["hash"], flipped["hash"]))
        self.assertIn(member, {ad_id for c in self._summary(scan_id)["clusters"] for ad_id in c["ad_ids"]})
        self.assertEqual(self._scan(scan_id)["snapshot_id"], back["snapshot_id"])
        self.assertEqual(len(self._snapshot_rows(scan_id)), 3)
        listed = self.client.get(f"/api/v1/competitor-scans/{scan_id}/snapshots").json()
        self.assertEqual({item["snapshot_id"]: item["current"] for item in listed["items"]},
                         {first: False, flipped["snapshot_id"]: False, back["snapshot_id"]: True})
        # The old snapshot is still readable as it was.
        old = self.client.get(f"/api/v1/competitor-scans/{scan_id}/snapshots/{flipped['snapshot_id']}").json()
        self.assertEqual(old["hash"], flipped["hash"])

    def test_the_hash_covers_observations_revisions_clusters_and_params(self):
        accepted = self._acme_and_self(keywords=[])
        self._drain()
        with tenant_session(self.ctx) as session:
            record = session.get(CompetitorScanRecord, accepted["scan_id"])
            inputs = snapshots.load_inputs(session, self.ctx, record)
            base = snapshots.content_hash(snapshots.content(record, inputs))
            self.assertEqual(base, snapshots.content_hash(snapshots.content(record, inputs)))  # pure
            self.assertEqual(base, session.get(CompetitorScanSnapshotRecord, record.snapshot_id).hash)
            params = copy.deepcopy(record.params_json)
            params["max_tagged_ads"] = 59
            session.expunge(record)
            record.params_json = params
            self.assertNotEqual(base, snapshots.content_hash(snapshots.content(record, inputs)))
            record.params_json = dict(record.params_json, max_tagged_ads=60)
            fewer = {**inputs, "ads": dict(list(inputs["ads"].items())[:-1])}
            self.assertNotEqual(base, snapshots.content_hash(snapshots.content(record, fewer)))
            no_clusters = {**inputs, "clusters": None}
            self.assertNotEqual(base, snapshots.content_hash(snapshots.content(record, no_clusters)))
            one = next(iter(inputs["relevance"]))
            fewer_judgments = {**inputs, "relevance": {k: v for k, v in inputs["relevance"].items() if k != one}}
            self.assertNotEqual(base, snapshots.content_hash(snapshots.content(record, fewer_judgments)))

    def test_rule_judgments_count_as_not_ai(self):
        accepted = self._start(self._body([self._confirmed(ACME)]))
        self._drain()
        scan_id = accepted["scan_id"]
        rule_ad = next(ad for ad in self._all_ads(scan_id) if ad["relevance"]["source"] == "rule")
        # A rule judged it unrelated, so it stays out of the snapshot; were a rule's verdict
        # "relevant", the snapshot would still not call it AI inference.
        content = self._refreeze(scan_id)["content"]
        self.assertNotIn(rule_ad["ad_id"], content["ads"])  # unrelated stays out of the snapshot
        with tenant_session(self.ctx) as session:
            record = session.get(CompetitorScanRecord, scan_id)
            inputs = snapshots.load_inputs(session, self.ctx, record)
            row = inputs["relevance"][rule_ad["ad_id"]]
            session.expunge(row)
            row.relevance = "relevant"  # as if the rule had said so: the summary must not call it AI
            judged = {**inputs["relevance"], row.competitor_ad_id: row}
            frozen = snapshots.content(record, {**inputs, "relevance": judged})
        entry = frozen["ads"][rule_ad["ad_id"]]
        self.assertEqual((entry["relevance_source"], entry["ai_inferred"]), ("rule", False))
        self.assertEqual(frozen["relevance_sources"]["rule"], 1)
        self.assertEqual(frozen["relevance_sources"]["non_ai"], frozen["relevance_sources"]["rule"]
                         + frozen["relevance_sources"]["user"])
        # A member's flip is not AI either.
        self._flip(scan_id, rule_ad["ad_id"], "relevant")
        summary = self._summary(scan_id, snapshot_id=self._refreeze(scan_id)["snapshot_id"])
        flipped = next(ad for ad in summary["unclustered"] if ad["ad_id"] == rule_ad["ad_id"])
        self.assertEqual((flipped["relevance_source"], flipped["ai_inferred"]), ("user", False))

    def test_snapshots_are_append_only(self):
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        for model, column in ((CompetitorScanSnapshotRecord, "hash"), (CompetitorScanAssessmentRecord, "kind")):
            for statement in (
                update(model).values(**{column: "x"}).where(model.scan_id == accepted["scan_id"]),
                model.__table__.delete().where(model.scan_id == accepted["scan_id"]),
            ):
                with self.assertRaises(DatabaseError):
                    with tenant_session(self.ctx) as session:
                        session.execute(statement)

    def test_the_latest_snapshot_of_a_product_in_a_market(self):
        first = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        second = self._start(self._body([self._confirmed(ACME)], keywords=[]))
        self._drain()
        with tenant_session(self.ctx) as session:
            latest = snapshots.latest_for_market(session, self.ctx, product_id=self.product_id, country="US")
            self.assertEqual(latest.id, self._scan(second["scan_id"])["snapshot_id"])
            self.assertNotEqual(latest.id, self._scan(first["scan_id"])["snapshot_id"])
            self.assertIsNone(snapshots.latest_for_market(session, self.ctx, product_id=self.product_id,
                                                          country="CA"))


# ---------------------------------------------------------------------------
# Scenario 7, second half, and the paid recluster
# ---------------------------------------------------------------------------


class ReclusterTests(SnapshotFixtures):
    def test_scenario_7_a_flipped_fsd_ad_is_unclustered_until_a_paid_recluster(self):
        accepted = self._start(self._body([self._confirmed(HALCYON)], keywords=[]))
        self._drain()
        scan_id = accepted["scan_id"]
        self.assertEqual(self._steps(scan_id)["cluster"], ("skipped", "no_relevant_observations"))
        self._problem(self.client.get(f"/api/v1/competitor-scans/{scan_id}/recluster-quote"),
                      409, "competitor_scan_nothing_to_cluster")
        fsd = self._all_ads(scan_id)[0]
        self.assertEqual(self._flip(scan_id, fsd["ad_id"], "relevant").status_code, 200)
        before = self._workspace_llm_attempts()
        self._refreeze(scan_id)  # free
        self.assertEqual(self._workspace_llm_attempts(), before)
        summary = self._summary(scan_id)
        self.assertEqual([ad["ad_id"] for ad in summary["unclustered"]], [fsd["ad_id"]])
        self.assertTrue(summary["can_recluster"])

        quote = self._recluster_quote(scan_id)  # Mock: nothing real, a simulated maximum
        self.assertEqual((quote["kind"], quote["execution_mode"], quote["max_usd"], quote["relevant_count"]),
                         ("recluster", "mock", "0", 1))
        self.assertEqual([(line["step"], line["calls"]) for line in quote["llm"]["lines"]], [("cluster", 1)])
        self.assertIsNotNone(quote["simulated_usd"])
        accepted_recluster = self._recluster(scan_id)
        [entry] = accepted_recluster["reclusters"]
        self.assertEqual(entry["status"], "queued")
        self.assertNotIn(entry["job_id"], (accepted["job_id"],))
        self._drain()
        [entry] = self._scan(scan_id)["reclusters"]
        self.assertEqual((entry["status"], entry["cluster_revision"]), ("completed", 1))
        after = self._summary(scan_id)
        self.assertEqual(after["snapshot_id"], entry["snapshot_id"])
        self.assertEqual(after["unclustered"], [])
        [cluster] = after["clusters"]
        self.assertTrue(cluster["claim"].startswith("Let the car do the driving"))
        self.assertEqual(cluster["ad_ids"], [fsd["ad_id"]])
        self.assertEqual(set(self._attempt_names(entry["job_id"])), {"cluster:1"})  # its own Job's name

    def test_a_byok_recluster_is_quoted_reserved_settled_and_frozen(self):
        self._bind_both()
        accepted = self._acme_and_self(keywords=[])
        self._drain()
        scan_id = accepted["scan_id"]
        first = self._scan(scan_id)
        quote = self._recluster_quote(scan_id)
        self.assertEqual((quote["execution_mode"], quote["llm"]["lines"][0]["max_input_tokens"]), ("byok", 100_000))
        self.assertGreater(Decimal(quote["max_usd"]), 0)
        scan = self._recluster(scan_id, key="recluster-1")
        [entry] = scan["reclusters"]
        job_id = entry["job_id"]
        reservations = self._scan_reservations(job_id)
        self.assertEqual(set(reservations), {llm_call_key(job_id, "cluster:1")})
        self.assertEqual({row.operation_id for row in reservations.values()}, {entry["operation_id"]})
        # Replayed by its key; a second one while it is queued is refused, and so is a resume.
        self.assertTrue(self._recluster(scan_id, key="recluster-1")["replayed"])
        self._problem(self.client.post(f"/api/v1/competitor-scans/{scan_id}/recluster",
                                       headers=self._headers("recluster-2")), 409, "competitor_scan_busy")
        self._problem(self.client.post(f"/api/v1/competitor-scans/{scan_id}/snapshots", headers=self._headers()),
                      409, "competitor_scan_busy")

        self._drain()
        scan = self._scan(scan_id)
        [entry] = scan["reclusters"]
        self.assertEqual((entry["status"], entry["cluster_revision"]), ("completed", 2))
        self.assertEqual([row.revision for row in self._cluster_rows(scan_id)], [1, 2])
        self.assertEqual(self._cluster_rows(scan_id)[1].created_by, "member_a")
        self.assertNotEqual(entry["snapshot_id"], first["snapshot_id"])  # a new revision, a new hash
        self.assertEqual(scan["snapshot_id"], entry["snapshot_id"])
        self.assertEqual(self._scan_reservations(job_id)[llm_call_key(job_id, "cluster:1")].state, "settled")
        with tenant_session(self.ctx) as session:
            ledger = set(session.scalars(select(ProviderCostLedgerEntryRecord.entry_type).where(
                ProviderCostLedgerEntryRecord.job_id == job_id)).all())
        self.assertTrue({"quote", "reservation", "reported_cost"} <= ledger, ledger)
        self.assertEqual(scan["cost"]["llm"]["calls"], first["cost"]["llm"]["calls"] + 1)
        self.assertEqual(self._attempt_names(job_id), {"cluster:1": "succeeded"})
        cluster_requests = [body for body in self.openai_requests
                            if body["response_format"]["json_schema"]["name"] == clusters.SCHEMA_NAME]
        self.assertEqual(len(cluster_requests), 2)  # the scan's and the recluster's

    def test_the_spend_guardrail_applies_to_a_recluster(self):
        self._bind_both()
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        quote = self._recluster_quote(accepted["scan_id"])
        with tenant_session(self.owner_ctx(self.ctx)) as session:
            from app.generation.spend import ensure_spend_guardrail

            guardrail = ensure_spend_guardrail(session, self.owner_ctx(self.ctx))
            guardrail.per_operation_limit = Decimal(quote["max_usd"]) - Decimal("0.0001")
        self._problem(self.client.post(f"/api/v1/competitor-scans/{accepted['scan_id']}/recluster",
                                       headers=self._headers()), 409, "guardrail_exceeded")
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.scalars(select(SpendReservationRecord.id).where(
                SpendReservationRecord.workspace_id == self.ctx.workspace_id,
                SpendReservationRecord.state == "reserved")).all(), [])
        self.assertEqual(self._scan(accepted["scan_id"])["reclusters"], [])


# ---------------------------------------------------------------------------
# Tag confirmations
# ---------------------------------------------------------------------------


class TagConfirmationTests(SnapshotFixtures):
    def _tagged_scan(self) -> tuple[str, dict]:
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        return accepted["scan_id"], next(ad for ad in self._all_ads(accepted["scan_id"]) if ad["tags"])

    def test_a_confirmation_is_a_new_revision_that_counts_as_confirmed(self):
        scan_id, ad = self._tagged_scan()
        before = self._refreeze(scan_id)["content"]
        suggested = {item["factor_id"]: item["value_id"] for item in ad["tags"]["factors"]}
        response = self._confirm_tags(scan_id, ad["ad_id"], [{"factor_id": "hook", "value_id": "v3"},
                                                             {"factor_id": "visual", "value_id": "none"}],
                                      key="tags-1")
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual((row["revision"], row["source"], row["ai_suggestion"], row["created_by"]),
                         (2, "user", False, "member_a"))
        self.assertEqual(row["previous"], {"revision": 1, "source": "ai"})
        self.assertEqual({item["factor_id"]: (item["value_id"], item["value_label"]) for item in row["factors"]},
                         {"hook": ("v3", "Price"), "visual": (None, None)})
        history = self._tags(scan_id, ad["ad_id"])
        self.assertEqual([item["source"] for item in history["items"]], ["ai", "user"])
        self.assertEqual(history["current"]["assessment_id"], row["assessment_id"])
        self.assertEqual([f["factor_id"] for f in history["vocabulary"]["factors"]], ["hook", "visual"])
        listed = next(item for item in self._all_ads(scan_id) if item["ad_id"] == ad["ad_id"])
        self.assertEqual((listed["tags"]["source"], listed["tags"]["ai_suggestion"]), ("user", False))

        # Replays by key; the key is refused for other tags.
        again = self._confirm_tags(scan_id, ad["ad_id"], [{"factor_id": "hook", "value_id": "v3"},
                                                          {"factor_id": "visual", "value_id": None}], key="tags-1")
        self.assertEqual((again.status_code, again.json()["replayed"], again.json()["revision"]), (200, True, 2))
        self._problem(self._confirm_tags(scan_id, ad["ad_id"], [{"factor_id": "hook", "value_id": "v1"}],
                                         key="tags-1"), 409, "idempotency_key_reused")

        # The next freeze counts it as confirmed, no longer as an AI suggestion.
        after = self._refreeze(scan_id)["content"]
        self.assertNotEqual(after, before)

        def column(content, value_id, name):
            hook = next(item for item in content["dimension_counts"] if item["factor_id"] == "hook")
            return next(v[name] for v in hook["values"] if v["value_id"] == value_id)

        self.assertEqual(column(after, "v3", "confirmed"), column(before, "v3", "confirmed") + 1)
        self.assertEqual(column(after, suggested["hook"], "suggested"),
                         column(before, suggested["hook"], "suggested") - 1)
        with tenant_session(self.ctx) as session:
            events = session.scalars(select(AuditEventRecord.initiator_id).where(
                AuditEventRecord.workspace_id == self.ctx.workspace_id,
                AuditEventRecord.event_type == "competitor_ad.tags_confirmed")).all()
        self.assertEqual(events, ["member_a"])

    def test_tags_are_checked_against_the_frozen_vocabulary(self):
        scan_id, ad = self._tagged_scan()
        self._problem(self._confirm_tags(scan_id, ad["ad_id"], [{"factor_id": "tone", "value_id": "v1"}]),
                      422, "unknown_factor")
        self._problem(self._confirm_tags(scan_id, ad["ad_id"], [{"factor_id": "hook", "value_id": "v9"}]),
                      422, "unknown_value")
        self._problem(self._confirm_tags(scan_id, ad["ad_id"], [{"factor_id": "hook", "value_id": "v1"},
                                                                {"factor_id": "hook", "value_id": "v2"}]),
                      422, "duplicate_factor")
        self.assertEqual(len(self._tags(scan_id, ad["ad_id"])["items"]), 1)  # nothing appended

    def test_an_observation_a_member_tagged_first_is_not_sent_to_the_model(self):
        self._bind_both()
        self.openai_answers = [None, REJECTED]  # filter:1 answered, tag:1 refused
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        scan_id = accepted["scan_id"]
        state = self._record(scan_id).evidence_json["tag"]
        second = state["assignments"]["tag:2"]["ad_id"]
        self.assertEqual(self._confirm_tags(scan_id, second, [{"factor_id": "hook", "value_id": "v2"}]).status_code,
                         200)
        resumed = self._resume(scan_id)
        self._drain()
        state = self._record(scan_id).evidence_json["tag"]
        self.assertEqual(state["calls"]["tag:2"]["status"], "superseded")
        self.assertNotIn("tag:2", self._attempt_names(resumed["job_id"]))
        self.assertEqual(self._tags(scan_id, second)["current"]["source"], "user")
        self.assertEqual(self._scan_reservations(resumed["job_id"])[llm_call_key(resumed["job_id"], "tag:2")].state,
                         "released")


# ---------------------------------------------------------------------------
# Resume finishes the judgment steps (the 2026-10-31 decision on #272)
# ---------------------------------------------------------------------------


class JudgmentResumeTests(SnapshotFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def test_a_key_refused_mid_filter_resumes_only_the_unanswered_batches(self):
        self.openai_answers = [None, REJECTED]  # filter:1 answered, filter:2 refused
        accepted = self._acme_and_self()
        self._drain()
        scan_id, job_id = accepted["scan_id"], accepted["job_id"]
        scan = self._scan(scan_id)
        self.assertEqual(scan["status"], "completed")  # every page is fetched
        steps = self._steps(scan_id)
        self.assertEqual(steps["filter"], ("partial", "credential_rejected"))
        self.assertEqual((steps["tag"], steps["cluster"]),
                         (("failed", "credential_rejected"), ("failed", "credential_rejected")))
        self.assertEqual(steps["freeze"], ("completed", None))  # what was judged is frozen
        self.assertTrue(scan["can_resume"])
        frozen = self._record(scan_id).evidence_json["filter"]
        self.assertGreaterEqual(len(frozen["assignments"]), 2)
        self.assertEqual(frozen["answered"], ["filter:1"])
        self.assertEqual(self._open_reservations(job_id), [])

        quote = self._resume_quote(scan_id)
        self.assertEqual(quote["market_data"]["max_credits"], 0)  # no page is left
        lines = {line["step"]: line["calls"] for line in quote["llm"]["lines"]}
        self.assertEqual(lines["filter"], len(frozen["assignments"]) - 1)
        self.assertEqual((lines["tag"], lines["cluster"]), (60, 1))
        # Discovery stopped on the same refusal before freezing its table, so it is owed too (#276).
        discover = self._record(scan_id).evidence_json["discover"]
        self.assertEqual((discover["status"], discover["reason"]), ("failed", "credential_rejected"))
        self.assertNotIn("assignments", discover)
        self.assertGreater(lines["discover"], 0)
        resumed = self._resume(scan_id)
        reserved = {name for name in [f"filter:{i}" for i in range(1, 100)]
                    if llm_call_key(resumed["job_id"], name) in self._scan_reservations(resumed["job_id"])}
        self.assertEqual(reserved, set(frozen["assignments"]) - {"filter:1"})
        self._drain()
        scan = self._scan(scan_id)
        self.assertEqual(self._steps(scan_id), {
            "fetch": ("completed", None), "media": ("completed", None), "filter": ("completed", None),
            "tag": ("completed", None), "cluster": ("completed", None), "freeze": ("completed", None),
        })
        self.assertEqual(self._all_ads(scan_id, relevance="unassessed"), [])
        names = self._attempt_names(resumed["job_id"])
        self.assertNotIn("filter:1", names)  # an answered ordinal is read, never called again
        self.assertEqual({name for name in names if name.startswith("filter:")},
                         set(frozen["assignments"]) - {"filter:1"})
        self.assertEqual(self._open_reservations(job_id, resumed["job_id"]), [])
        self.assertFalse(scan["can_resume"])
        self._problem(self.client.get(f"/api/v1/competitor-scans/{scan_id}/resume-quote"),
                      409, "competitor_scan_not_resumable")

    def test_a_key_refused_mid_tag_resumes_only_the_untagged_ordinals(self):
        self.openai_answers = [None, None, None, REJECTED]  # filter:1, tag:1, tag:2 answered; tag:3 refused
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        scan_id, job_id = accepted["scan_id"], accepted["job_id"]
        steps = self._steps(scan_id)
        self.assertEqual(steps["tag"], ("partial", "credential_rejected"))
        self.assertEqual(steps["cluster"], ("failed", "credential_rejected"))
        table = self._record(scan_id).evidence_json["tag"]
        self.assertEqual(table["answered"], ["tag:1", "tag:2"])
        self.assertEqual(self._open_reservations(job_id), [])
        relevant = len(table["assignments"])

        quote = self._resume_quote(scan_id)
        lines = {line["step"]: line["calls"] for line in quote["llm"]["lines"]}
        self.assertEqual(lines, {"tag": relevant - 2, "cluster": 1})  # nothing else is owed
        resumed = self._resume(scan_id)
        self._drain()
        names = self._attempt_names(resumed["job_id"])
        self.assertEqual(set(names), {f"tag:{i}" for i in range(3, relevant + 1)} | {"cluster:1"})
        self.assertEqual(self._record(scan_id).evidence_json["tag"]["assignments"], table["assignments"])
        self.assertEqual(self._steps(scan_id)["tag"], ("completed", None))
        self.assertEqual(len([ad for ad in self._all_ads(scan_id) if ad["tags"]]), relevant)
        self.assertEqual(self._open_reservations(job_id, resumed["job_id"]), [])

    def test_a_resume_with_pages_left_still_reserves_every_unanswered_ordinal(self):
        self.mock_credits = 4  # Acme Charge's three pages and Brightvolt's one; Halcyon Motors' first page gets a 402
        accepted = self._start(self._body())
        self._drain()
        state = self._record(accepted["scan_id"]).evidence_json
        quote = self._resume_quote(accepted["scan_id"])
        lines = {line["step"]: line["calls"] for line in quote["llm"]["lines"]}
        unanswered_filter = scan_plan.filter_calls(scan_plan.ScanParams.from_json(
            self._record(accepted["scan_id"]).params_json)) - len(state["filter"]["answered"])
        self.assertEqual(lines["filter"], unanswered_filter)
        self.assertEqual(lines["tag"], 60 - len(state["tag"]["answered"]))
        self.assertEqual(lines["discover"], 30)
        self.assertNotIn("cluster", lines)  # cluster:1 was answered: read, never asked again


# ---------------------------------------------------------------------------
# The product's drafting switch
# ---------------------------------------------------------------------------


class ProductSwitchTests(SnapshotFixtures):
    def test_product_reads_and_inserts_never_name_the_new_column(self):
        """The Release service adds the column while the new API already runs (#283):
        only the switch's own routes may touch it."""
        self.assertNotIn("competitor_snapshot_in_drafts", str(select(ProductRecord)))
        column = ProductRecord.__table__.c.competitor_snapshot_in_drafts
        self.assertIsNone(column.default)  # no client-side value in an INSERT
        self.assertIsNotNone(column.server_default)
        self.assertTrue(ProductRecord.__mapper__.column_attrs["competitor_snapshot_in_drafts"].deferred)

    def test_any_member_switches_it_and_it_reads_back(self):
        self.assertEqual(self._settings().json()["competitor_snapshot_in_drafts"], True)  # on by default
        off = self._switch(False, key="switch-1")
        self.assertEqual((off.status_code, off.json()["competitor_snapshot_in_drafts"]), (200, False), off.text)
        self.assertEqual(self._settings().json()["competitor_snapshot_in_drafts"], False)
        self.assertTrue(self._switch(False, key="switch-1").json()["replayed"])
        self._problem(self._switch(True, key="switch-1"), 409, "idempotency_key_reused")
        self.current = self.owner_ctx(self.ctx)
        self.assertEqual(self._switch(True, key="switch-2").json()["competitor_snapshot_in_drafts"], True)
        self.current = self.ctx
        with tenant_session(self.ctx) as session:
            self.assertTrue(session.get(ProductRecord, self.product_id).competitor_snapshot_in_drafts)
            events = session.scalars(select(AuditEventRecord.details_json).where(
                AuditEventRecord.workspace_id == self.ctx.workspace_id,
                AuditEventRecord.event_type == "product.competitor_settings_updated",
            ).order_by(AuditEventRecord.created_at)).all()
        self.assertEqual([(e["previous"], e["competitor_snapshot_in_drafts"]) for e in events],
                         [(True, False), (False, True)])


# ---------------------------------------------------------------------------
# Another Workspace
# ---------------------------------------------------------------------------


class IsolationTests(SnapshotFixtures):
    def test_another_workspace_reads_and_changes_nothing(self):
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        scan_id = accepted["scan_id"]
        scan = self._scan(scan_id)
        ad = next(ad for ad in self._all_ads(scan_id) if ad["tags"])
        stranger = self._make_context(f"ws_snap_other_{self.ctx.workspace_id[-6:]}", role="member",
                                      actor="member_b")
        for current in (stranger, self.owner_ctx(stranger)):
            self.current = current
            for response in (
                self.client.get(f"/api/v1/competitor-scans/{scan_id}/summary"),
                self.client.get(f"/api/v1/competitor-scans/{scan_id}/snapshots"),
                self.client.get(f"/api/v1/competitor-scans/{scan_id}/snapshots/{scan['snapshot_id']}"),
                self.client.post(f"/api/v1/competitor-scans/{scan_id}/snapshots", headers=self._headers()),
                self.client.post(f"/api/v1/competitor-scans/{scan_id}/ads/{ad['ad_id']}/tags",
                                 json={"tags": [{"factor_id": "hook", "value_id": "v1"}]}, headers=self._headers()),
                self.client.get(f"/api/v1/competitor-scans/{scan_id}/ads/{ad['ad_id']}/tags"),
                self.client.get(f"/api/v1/competitor-scans/{scan_id}/recluster-quote"),
                self.client.post(f"/api/v1/competitor-scans/{scan_id}/recluster", headers=self._headers()),
            ):
                self._problem(response, 404, "competitor_scan_not_found")
            self._problem(self._settings(), 404, "product_not_found")
            self._problem(self._switch(False), 404, "product_not_found")
        self.current = self.ctx
        self._problem(self.client.get(f"/api/v1/competitor-scans/{scan_id}/snapshots/csnap_unknown"),
                      404, "competitor_snapshot_not_found")
        self.assertEqual(len(self._tags(scan_id, ad["ad_id"])["items"]), 1)
        self.assertEqual(self._scan(scan_id)["reclusters"], [])
        self.assertTrue(self._settings().json()["competitor_snapshot_in_drafts"])
        self.assertEqual(len(self._snapshot_rows(scan_id)), 1)
