"""Ads-first ideas (ADR 0018, #189): the container, its read endpoints and the keep mark."""
import unittest

from pydantic import ValidationError

from app.adapters.persistence.database import tenant_session
from app.marketing.ideas_contracts import Ideas, IdeasProjectDetail
from app.marketing.models import MarketingProjectRecord
from tests.test_marketing_planning import MarketingPlanningFixtures


def ideas_example():
    """Two buying claims for Acme Charge, two ways of telling each, one ad per way."""
    def expression(eid, mechanism):
        return {"id": eid, "name": f"Telling {eid}", "description": "Show the charger at work.",
                "fit_reason": "Travellers carry one charger.", "how_to_tell": "Follow one trip from desk to gate.",
                "visual_mechanism": mechanism, "evidence_basis": {"fact_ids": ["f1"]}}

    def combination(cid, angle_id, expression_id):
        return {"id": cid, "angle_id": angle_id, "expression_id": expression_id,
                "headline": "One charger for the whole trip", "body_copy": "Acme Charge powers laptop and phone together.",
                "visual_brief": "A traveller plugs one charger in at the gate.", "checks": ["Logo is readable"]}

    return {
        "market": "United States", "ad_language": "en-US", "strategy_language": "en-US",
        "angles": [
            {"id": "a1", "proposition": "One charger replaces three", "rationale": "Travellers pack light.",
             "fact_ids": ["f1"], "expressions": [expression("e1", "scenario_story"), expression("e2", "route_map")]},
            {"id": "a2", "proposition": "Charges a laptop at full speed", "rationale": "65W covers laptops.",
             "fact_ids": ["f1"], "expressions": [expression("e3", "live_connection_demo"), expression("e4", "qa_objection")]},
        ],
        "combinations": [combination("c1", "a1", "e1"), combination("c2", "a1", "e2"),
                         combination("c3", "a2", "e3"), combination("c4", "a2", "e4")],
    }


class IdeasDocumentTests(unittest.TestCase):
    def test_two_claims_with_two_ways_each_are_valid(self):
        doc = Ideas.model_validate(ideas_example())
        self.assertEqual([c.id for c in doc.combinations], ["c1", "c2", "c3", "c4"])

    def test_combination_must_point_at_an_expression_of_its_angle(self):
        doc = ideas_example()
        doc["combinations"][0]["expression_id"] = "missing"
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_combination_cannot_borrow_another_angles_expression(self):
        doc = ideas_example()
        doc["combinations"][0]["expression_id"] = "e3"
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_every_expression_needs_an_ad(self):
        doc = ideas_example()
        doc["combinations"] = [c for c in doc["combinations"] if c["id"] != "c4"]
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_ids_are_unique(self):
        doc = ideas_example()
        doc["combinations"][1]["id"] = "c1"
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_one_angle_is_not_enough(self):
        doc = ideas_example()
        doc["angles"] = doc["angles"][:1]
        doc["combinations"] = [c for c in doc["combinations"] if c["angle_id"] == doc["angles"][0]["id"]]
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_ad_language_uses_the_plan_pattern(self):
        doc = ideas_example()
        doc["ad_language"] = "English"
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_detail_states_are_only_the_three_ads_first_states(self):
        detail = {"id": "mp_1", "revision": 1, "state": "input", "product_id": "prod_1", "market": "United States",
                  "ad_language": "en-US", "understanding": None, "ideas": None, "latest_call": None, "ads": [], "pending": []}
        IdeasProjectDetail.model_validate(detail)
        with self.assertRaises(ValidationError):
            IdeasProjectDetail.model_validate({**detail, "state": "plan_confirmed"})


class MarketingIdeasSchemaTests(MarketingPlanningFixtures):
    def test_existing_projects_are_plan_first(self):
        old = self.create()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, old["id"])
            self.assertEqual(row.flow, "plan_first")
            self.assertIsNone(row.ideas_json)
            self.assertIsNone(row.market)
            self.assertIsNone(row.ad_language)
