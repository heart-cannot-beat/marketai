"""Ads-first ideas (ADR 0018, #189): the container, its read endpoints and the keep mark."""
from app.adapters.persistence.database import tenant_session
from app.marketing.models import MarketingProjectRecord
from tests.test_marketing_planning import MarketingPlanningFixtures


class MarketingIdeasSchemaTests(MarketingPlanningFixtures):
    def test_existing_projects_are_plan_first(self):
        old = self.create()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, old["id"])
            self.assertEqual(row.flow, "plan_first")
            self.assertIsNone(row.ideas_json)
            self.assertIsNone(row.market)
            self.assertIsNone(row.ad_language)
