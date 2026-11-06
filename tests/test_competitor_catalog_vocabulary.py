"""Issue #329 -- the expression mechanism catalog is every product's tag vocabulary.

A marketing project's product anchor (``confirm_understanding``) has no
published library version, so it can never get a test plan (``create_plan``
refuses it with 409 ``product_not_published``, and since #343 the route is
frozen for every product); before #329 every scan
of it froze an empty vocabulary and skipped tagging (``no_vocabulary``). The
vocabulary now ends with one factor from ``expression-catalog-v1`` for every
product: its values are exactly the catalog's mechanism keys, members are
``(catalog, version, mechanism key)``, labels are in the language the
vocabulary is frozen in, and the tag prompt carries ``how_to_tell``.

Real HTTP routes and the real Worker, as the C-23 tests run them, but with the
product's real vocabulary (no patch). Covered: a Mock scan (the Mock Ad
Library, the Controlled Mock tag fixture) of a marketing-project product tags
its relevant observations against the catalog, a member confirms a suggestion
and the summary counts it; a zh-CN scan labels the catalog in Chinese; a
BYOK scan (a fake OpenAI: no request leaves) reserves, sends and settles
``tag:<i>`` with the catalog and its guidance within the unchanged
18000 / 2000 token budget; an intake product with a ready test plan gets its
plan factors and then the catalog's; the catalog keeps its slot under
``MAX_FACTORS``; a frozen vocabulary is never rebuilt; the planner's catalog
reference is unchanged.
"""
from __future__ import annotations

import dataclasses
import json
from unittest.mock import patch

from tests.test_competitor_snapshots import VOCABULARY
from tests.test_competitors_api import BRIGHTVOLT, ACME
from tests.test_marketing_competitor_review import CompetitorPlanFixtures
from tests.test_test_plans_api import PlanFixtures

from app.adapters.persistence.database import tenant_session
from app.competitors import scan_plan, scan_service, tags
from app.competitors.models import CompetitorAdRecord, CompetitorScanRecord
from app.external_creatives import vocabulary as vocab
from app.marketing import expression_catalog
from app.providers.llm_chat import estimate_request_input_tokens, llm_call_key

CATALOG = vocab.CATALOG_FACTOR_ID
KEYS = [entry["visual_mechanism"] for entry in expression_catalog.ENTRIES]


class CatalogVocabularyFixtures(CompetitorPlanFixtures):
    real_vocabulary = True

    def _marketing_product(self) -> str:
        """The product a marketing project binds on ``confirm_understanding``: no test plan, ever."""
        product_id = self._understood()["product_id"]
        # ``self._create`` adds a competitor here; the plan command is PlanFixtures'.
        refused = PlanFixtures._create(self, {"product_id": product_id}, expect=409)
        self.assertEqual(refused["code"], "product_not_published")
        return product_id

    def _scanned(self) -> dict:
        self._marketing_product()
        accepted = self._start(self._body([self._confirmed(ACME), self._confirmed(BRIGHTVOLT, role="self")],
                                          keywords=[]))
        self._drain()
        return accepted

    def assertCatalog(self, factor: dict, language: str = "en") -> None:
        self.assertEqual((factor["factor_id"], factor["source"], factor["label"]),
                         (CATALOG, vocab.SOURCE_CATALOG, expression_catalog.FACTOR_NAMES[language]))
        self.assertEqual([value["label"] for value in factor["values"]],
                         [expression_catalog.DISPLAY_NAMES[key][language] for key in KEYS])


class MarketingProductScanTests(CatalogVocabularyFixtures):
    def test_a_mock_scan_of_a_marketing_project_product_tags_against_the_catalog(self):
        accepted = self._scanned()
        scan_id = accepted["scan_id"]
        self.assertEqual(self._steps(scan_id)["tag"], ("completed", None))  # was skipped: no_vocabulary

        state = self._record(scan_id).evidence_json["tag"]
        [factor] = state["vocabulary"]["factors"]
        self.assertCatalog(factor)
        self.assertEqual(factor["catalog_version"], expression_catalog.CATALOG_VERSION)
        self.assertEqual([value["value_id"] for value in factor["values"]], [f"v{i}" for i in range(1, 11)])
        self.assertEqual([value["members"] for value in factor["values"]], [
            [{"catalog": "expression_catalog", "catalog_version": "expression-catalog-v1", "visual_mechanism": key}]
            for key in KEYS
        ])
        self.assertEqual([value["guidance"] for value in factor["values"]],
                         [entry["how_to_tell"] for entry in expression_catalog.ENTRIES])
        self.assertNotIn("other:", json.dumps(factor))
        self.assertEqual(state["vocabulary_hash"], tags.canonical_hash(state["vocabulary"]))

        # Every relevant observation of the table carries a Controlled Mock suggestion.
        ads = self._all_ads(scan_id)
        tagged = [ad for ad in ads if ad["tags"]]
        self.assertEqual({ad["ad_id"] for ad in tagged}, {entry["ad_id"] for entry in state["assignments"].values()})
        self.assertTrue(tagged)
        values = {value["value_id"] for value in factor["values"]}
        for ad in tagged:
            self.assertTrue(ad["tags"]["ai_suggestion"])
            [suggested] = ad["tags"]["factors"]
            self.assertEqual(suggested["factor_id"], CATALOG)
            self.assertIn(suggested["value_id"], values)
        history = self._tags(scan_id, tagged[0]["ad_id"])
        self.assertCatalog(history["vocabulary"]["factors"][0])
        self.assertTrue(history["current"]["factors"][0]["reason"].startswith("Controlled Mock"))

        # A member confirms a different value: a revision the summary counts as confirmed.
        ad_id = tagged[0]["ad_id"]
        chosen = next(value for value in values if value != tagged[0]["tags"]["factors"][0]["value_id"])
        confirmed = self._confirm_tags(scan_id, ad_id, [{"factor_id": CATALOG, "value_id": chosen}])
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        current = self._tags(scan_id, ad_id)["current"]
        self.assertEqual((current["source"], current["factors"][0]["value_id"]), ("user", chosen))
        self.assertEqual(current["factors"][0]["factor_label"], "Expression mechanism")
        self._refreeze(scan_id)
        [counts] = self._summary(scan_id)["dimension_counts"]
        self.assertEqual(counts["factor_id"], CATALOG)
        self.assertEqual(next(v for v in counts["values"] if v["value_id"] == chosen)["confirmed"], 1)
        self.assertEqual(sum(v["suggested"] for v in counts["values"]) + counts["none"]["suggested"],
                         len(tagged) - 1)
        # Mechanism keys are no free text: an unknown value or an ``other:`` label is refused.
        self._problem(self._confirm_tags(scan_id, ad_id, [{"factor_id": CATALOG, "value_id": "route_map"}]),
                      422, "unknown_value")
        self._problem(self._confirm_tags(scan_id, ad_id, [{"factor_id": CATALOG, "value_id": "other:x"}]),
                      422, "unknown_value")

    def test_a_zh_cn_scan_labels_the_catalog_in_chinese(self):
        self.current = dataclasses.replace(self.ctx, ui_language="zh-CN")
        accepted = self._scanned()
        self.current = self.ctx
        [factor] = self._record(accepted["scan_id"]).evidence_json["tag"]["vocabulary"]["factors"]
        self.assertCatalog(factor, "zh-CN")
        self.assertEqual(factor["label"], "表达机制")
        ad = next(ad for ad in self._all_ads(accepted["scan_id"]) if ad["tags"])
        current = self._tags(accepted["scan_id"], ad["ad_id"])["current"]
        self.assertEqual(current["language"], "zh-CN")
        self.assertEqual(current["factors"][0]["factor_label"], "表达机制")
        self.assertIn(current["factors"][0]["value_label"], {names["zh-CN"] for names in
                                                             expression_catalog.DISPLAY_NAMES.values()})

    def test_a_scan_frozen_before_329_keeps_its_empty_vocabulary(self):
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        empty = vocab.empty()
        with tenant_session(self.ctx) as session:
            record = session.get(CompetitorScanRecord, accepted["scan_id"])
            evidence = dict(record.evidence_json)
            evidence["tag"] = {**evidence["tag"], "vocabulary": empty, "vocabulary_hash": vocab.vocabulary_hash(empty)}
            record.evidence_json = evidence
        with tenant_session(self.ctx) as session:
            record = session.get(CompetitorScanRecord, accepted["scan_id"])
            rebuilt = scan_service._with_vocabulary(session, self.ctx, record)
        self.assertEqual(rebuilt["tag"]["vocabulary"], empty)  # never rebuilt with the catalog
        self._drain()
        self.assertEqual(self._steps(accepted["scan_id"])["tag"], ("skipped", "no_vocabulary"))
        self.assertEqual(self._record(accepted["scan_id"]).evidence_json["tag"]["vocabulary"], empty)


class ByokCatalogTagTests(CatalogVocabularyFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def test_tag_calls_send_the_catalog_and_its_guidance_within_the_reservation(self):
        accepted = self._scanned()
        job_id = accepted["job_id"]
        state = self._record(accepted["scan_id"]).evidence_json["tag"]
        names = list(state["assignments"])
        self.assertTrue(names)
        reservations = self._scan_reservations(job_id)
        self.assertEqual({reservations[llm_call_key(job_id, name)].state for name in names}, {"settled"})
        self.assertEqual(self._open_reservations(job_id), [])
        budget = scan_plan.LLM_BUDGETS["tag"]
        self.assertEqual((budget.max_input_tokens, budget.max_output_tokens), (18_000, 2_000))  # unchanged

        requests = [body for body in self.openai_requests
                    if body["response_format"]["json_schema"]["name"] == tags.SCHEMA_NAME]
        self.assertEqual(len(requests), len(names))
        for body in requests:
            self.assertEqual(body["max_completion_tokens"], budget.max_output_tokens)
            schema = body["response_format"]["json_schema"]["schema"]
            self.assertEqual(list(schema["properties"]["factors"]["properties"]), ["f1"])
            self.assertEqual(schema["properties"]["factors"]["properties"]["f1"]["properties"]["value_id"]["enum"],
                             [f"v{i}" for i in range(1, 11)] + [tags.NONE])
            text = next(part["text"] for part in body["messages"][-1]["content"] if part["type"] == "text")
            [factor] = json.loads(text.split("\n\n", 1)[1])["factors"]
            self.assertEqual(factor["factor"], "Expression mechanism")
            self.assertEqual(list(factor["how_to_tell"].values()),
                             [entry["how_to_tell"] for entry in expression_catalog.ENTRIES])
            self.assertIn("how_to_tell", body["messages"][0]["content"])
        ad = next(ad for ad in self._all_ads(accepted["scan_id"]) if ad["tags"])
        self.assertEqual(self._tags(accepted["scan_id"], ad["ad_id"])["current"]["evidence"]["prompt_version"],
                         tags.PROMPT_VERSION)


class VocabularyBuildTests(CatalogVocabularyFixtures):
    def _vocabulary(self, product_id: str, language: str = "en") -> dict:
        with tenant_session(self.ctx) as session:
            return vocab.build_vocabulary(session, self.ctx, product_id, language=language)

    def _ready_intake_plan(self) -> tuple[str, dict]:
        product_id = self._product()
        accepted = PlanFixtures._create(self, {"product_id": product_id})  # the domain: the route answers 410
        self._drain()
        plan = self.client.get(f"/api/v1/test-plans/{accepted['plan_id']}").json()
        self.assertEqual(plan["status"], "ready", plan.get("failure"))
        return product_id, plan

    def test_an_intake_product_with_a_ready_plan_gets_its_factors_and_the_catalog(self):
        product_id, plan = self._ready_intake_plan()
        built = self._vocabulary(product_id)
        plan_factors = [factor for factor in built["factors"] if factor["source"] == vocab.SOURCE_TEST_PLAN]
        self.assertTrue(plan_factors)
        self.assertEqual(built["factors"], plan_factors + [vocab.catalog_factor("en")])  # the catalog last
        self.assertEqual([item["plan_id"] for item in built["plans"]], [plan["plan_id"]])
        # A scan of that product freezes both, and tags each.
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT, product_id=product_id)], keywords=[],
                                          product_id=product_id))
        self._drain()
        frozen = self._record(accepted["scan_id"]).evidence_json["tag"]["vocabulary"]
        self.assertEqual([factor["source"] for factor in frozen["factors"]],
                         [vocab.SOURCE_TEST_PLAN] * len(plan_factors) + [vocab.SOURCE_CATALOG])
        ad = next(ad for ad in self._all_ads(accepted["scan_id"]) if ad["tags"])
        self.assertEqual([item["factor_id"] for item in ad["tags"]["factors"]],
                         [factor["factor_id"] for factor in frozen["factors"]])

    def test_the_catalog_keeps_its_slot_under_the_factor_cap(self):
        product_id, _plan = self._ready_intake_plan()
        with patch.object(vocab, "MAX_FACTORS", 2):
            built = self._vocabulary(product_id)
        self.assertEqual([factor["source"] for factor in built["factors"]],
                         [vocab.SOURCE_TEST_PLAN, vocab.SOURCE_CATALOG])
        with patch.object(vocab, "MAX_FACTORS", 1):
            self.assertEqual([factor["factor_id"] for factor in self._vocabulary(product_id)["factors"]], [CATALOG])

    def test_labels_follow_the_language_and_identity_does_not(self):
        product_id = self._product()
        english, chinese = self._vocabulary(product_id), self._vocabulary(product_id, "zh-CN")
        self.assertCatalog(english["factors"][0])
        self.assertCatalog(chinese["factors"][0], "zh-CN")
        for language in ("en", "fr"):  # an unknown language falls back to English
            self.assertCatalog(self._vocabulary(product_id, language)["factors"][0])
        identity = [[(v["value_id"], v["members"], v["guidance"]) for v in built["factors"][0]["values"]]
                    for built in (english, chinese)]
        self.assertEqual(identity[0], identity[1])
        # Public views show labels only: no members, no guidance.
        public = vocab.public_view(english)["factors"][0]
        self.assertEqual(set(public["values"][0]), {"value_id", "label"})
        self.assertEqual(public["source"], "expression_catalog")


class CatalogPromptTests(CatalogVocabularyFixtures):
    def test_the_planner_reference_and_entries_are_unchanged(self):
        # Display names live apart from ENTRIES: the planner's frozen reference (and so every
        # planning, plan review, intake and regeneration prompt built from it) is what it was.
        reference = expression_catalog.planner_reference()
        self.assertEqual(reference["version"], "expression-catalog-v1")
        self.assertEqual({tuple(sorted(entry)) for entry in reference["entries"]},
                         {("how_to_tell", "name", "source", "suited_evidence", "visual_mechanism")})
        self.assertEqual([entry["name"] for entry in reference["entries"]],
                         [expression_catalog.DISPLAY_NAMES[key]["en"] for key in KEYS])
        self.assertEqual(set(expression_catalog.DISPLAY_NAMES), expression_catalog.mechanism_keys())

    def test_the_web_mechanism_copy_matches_the_display_names(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "web" / "locales"
        for language in ("en", "zh-CN"):
            messages = json.loads((root / language / "common.json").read_text())
            self.assertEqual({key: messages[f"marketing-visual-mechanism-{key}"] for key in KEYS},
                             {key: names[language] for key, names in expression_catalog.DISPLAY_NAMES.items()})

    def test_the_worst_vocabulary_still_fits_the_tag_reservation(self):
        def ad() -> CompetitorAdRecord:
            cards = [{"index": index, "title": "T" * 300, "body": "B" * 2_000, "link_description": "L" * 300,
                      "cta_text": "Shop now"} for index in range(6)]
            return CompetitorAdRecord(advertiser_name="A" * 200, display_format="DCO",
                                      landing_domain="example.com", cards_json=cards)

        images = [{"card_index": index, "object_key": f"k{index}", "storage_version_id": "s", "byte_size": 1,
                   "sha256_b64": "", "mime_type": "image/jpeg"} for index in range(3)]
        budget = scan_plan.LLM_BUDGETS["tag"].max_input_tokens
        plan_factors = [{"factor_id": f"k{i}", "label": "因" * 80, "source": "test_plan",
                         "values": [{"value_id": f"v{j}", "label": "值" * 120, "members": []} for j in range(1, 17)]}
                        for i in range(vocab.MAX_FACTORS - 1)]
        for language in ("en", "zh-CN"):
            # The main flow (catalog only) keeps every image of the longest ad.
            request, chosen = tags.build_request(ad(), {"factors": [vocab.catalog_factor(language)]}, images,
                                                 language=language, execution_mode="byok")
            self.assertEqual(len(chosen), scan_plan.TAG_MAX_IMAGES)
            self.assertLessEqual(estimate_request_input_tokens(request, execution_mode="byok"), budget)
            # Eleven full plan factors and the catalog: still within the reservation.
            request, _chosen = tags.build_request(
                ad(), {"factors": plan_factors + [vocab.catalog_factor(language)]}, images,
                language=language, execution_mode="byok")
            self.assertLessEqual(estimate_request_input_tokens(request, execution_mode="byok"), budget)
        # A plan vocabulary's request has no guidance key (its prompt is as before).
        request, _chosen = tags.build_request(ad(), VOCABULARY, images, language="en", execution_mode="byok")
        document = json.loads(request.messages[-1].text.split("\n\n", 1)[1])
        self.assertTrue(all("how_to_tell" not in factor for factor in document["factors"]))
