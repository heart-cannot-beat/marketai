"""One OpenAI key runs planning and vision checks when the deployment prefers Gemini (#192, ads-first S5).

Nothing here calls a provider: capabilities are read over HTTP or directly, and the one paid planning call is
only admitted (queued with its frozen provider), never executed.
"""
import json
import os
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import utcnow
from app.entitlements.cost_models import SpendGuardrailRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import CredentialProtector
from app.entitlements.models import EntitlementProjectionRecord
from app.marketing import ai_qc
from app.marketing.models import PlanningCallRecord
from tests.test_marketing_planning import MarketingPlanningFixtures

SETTINGS = [f"{prefix}_{suffix}" for prefix in ("MARKETAI_PLANNING", "MARKETAI_MARKETING_QC")
            for suffix in ("PROVIDER", "MODEL", "MAX_COST_USD", "MODELS_JSON")]
GEMINI_DEPLOYMENT = {"MARKETAI_PLANNING_PROVIDER": "gemini", "MARKETAI_PLANNING_MODEL": "gemini-text-test",
                     "MARKETAI_PLANNING_MAX_COST_USD": "0.20"}
TABLE = json.dumps({"openai": {"model": "openai-text-test", "max_cost_usd": "0.10"}})
QC_GEMINI_DEPLOYMENT = {"MARKETAI_MARKETING_QC_PROVIDER": "gemini", "MARKETAI_MARKETING_QC_MODEL": "gemini-vision-test",
                        "MARKETAI_MARKETING_QC_MAX_COST_USD": "0.20"}
QC_TABLE = json.dumps({"openai": {"model": "vision-test", "max_cost_usd": "0.10"}})


@contextmanager
def deployment(env):
    """Exactly these planning and QC settings; every other one of the eight is unset."""
    with patch.dict(os.environ, {}):
        for name in SETTINGS:
            os.environ.pop(name, None)
        os.environ.update(env)
        yield


class ProviderChoiceFixtures(MarketingPlanningFixtures):
    def eligible(self, ctx, *providers):
        """Entitled, with a USD guardrail, and a generation key bound for each provider named."""
        owner = self.owner_ctx(ctx)
        with tenant_session(ctx) as session:
            admin = CredentialAdmin(CredentialProtector.local_only())
            for provider in providers:
                credential = admin.create(session, owner, credential_key=f"{provider}_api_key", provider=provider,
                                          plaintext=f"fixture-{provider}-key".encode())
                admin.bind(session, owner, binding_scope="generation", provider=provider,
                           credential_version_id=credential.credential_version_id)
            session.merge(EntitlementProjectionRecord(workspace_id=ctx.workspace_id, entitlement_version=1,
                lease_sequence=1, semantic_hash="provider-choice-test", issued_at=utcnow(),
                valid_until=utcnow() + timedelta(days=1),
                resolved_entitlements_json={"controlled_mock": True, "storage.bytes.max": 10 * 1024 ** 3}))
            session.add(SpendGuardrailRecord(workspace_id=ctx.workspace_id, budget_currency="USD",
                per_operation_limit=Decimal("1"), rolling_24h_limit=Decimal("5"), period_limit=Decimal("5"),
                outstanding_exposure_limit=Decimal("5"), created_at=utcnow(), updated_at=utcnow(), updated_by="owner"))

    def capabilities(self, env):
        with deployment(env):
            response = self.client.get("/api/v1/marketing/capabilities")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["paid"]

    def qc(self, env, mode="byok"):
        with deployment(env), tenant_session(self.context) as session:
            return ai_qc.capability(session, self.context, SimpleNamespace(execution_mode=mode))


class TestPlanningProviderChoice(ProviderChoiceFixtures):
    def test_an_openai_key_alone_plans_when_the_deployment_prefers_gemini(self):
        self.eligible(self.context, "openai")
        paid = self.capabilities({**GEMINI_DEPLOYMENT, "MARKETAI_PLANNING_MODELS_JSON": TABLE})
        self.assertEqual((paid["available"], paid["provider"], paid["model"], paid["maximum_cost"]),
                         (True, "openai", "openai-text-test", "0.10"))
        self.assertIsNone(paid["unavailable_reason"])

    def test_the_fallback_is_frozen_on_the_paid_call(self):
        self.eligible(self.context, "openai")
        project = self.create()
        env = {**GEMINI_DEPLOYMENT, "MARKETAI_PLANNING_MODELS_JSON": TABLE}
        with deployment(env):
            paid = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
            call = self.plan_call(project, mode="byok", quote_hash=paid["quote_hash"])
        with tenant_session(self.context) as session:
            row = session.get(PlanningCallRecord, call["id"])
            self.assertEqual((row.execution_mode, row.provider, row.model, str(row.maximum_cost)),
                             ("byok", "openai", "openai-text-test", "0.10"))

    def test_both_keys_bound_keeps_the_deployment_provider(self):
        self.eligible(self.context, "openai", "gemini")
        paid = self.capabilities({**GEMINI_DEPLOYMENT, "MARKETAI_PLANNING_MODELS_JSON": TABLE})
        self.assertEqual((paid["available"], paid["provider"], paid["model"], paid["maximum_cost"]),
                         (True, "gemini", "gemini-text-test", "0.20"))

    def test_without_the_table_the_answer_is_unchanged(self):
        self.eligible(self.context, "openai")
        paid = self.capabilities(GEMINI_DEPLOYMENT)
        self.assertEqual((paid["available"], paid["unavailable_reason"], paid["provider"], paid["model"],
                          paid["maximum_cost"]), (False, "credential_required", "gemini", "gemini-text-test", "0"))

    def test_legacy_settings_win_over_the_table_for_the_deployment_provider(self):
        self.eligible(self.context, "openai")
        paid = self.capabilities({"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODEL": "text-test",
                                  "MARKETAI_PLANNING_MAX_COST_USD": "0.10", "MARKETAI_PLANNING_MODELS_JSON":
                                  json.dumps({"openai": {"model": "other-model", "max_cost_usd": "0.30"}})})
        self.assertEqual((paid["available"], paid["provider"], paid["model"], paid["maximum_cost"]),
                         (True, "openai", "text-test", "0.10"))

    def test_an_invalid_legacy_setting_is_not_rescued_by_the_table(self):
        self.eligible(self.context, "openai")
        paid = self.capabilities({"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MAX_COST_USD": "0.10",
                                  "MARKETAI_PLANNING_MODELS_JSON": TABLE})
        self.assertEqual((paid["available"], paid["unavailable_reason"]), (False, "planner_not_configured"))

    def test_the_table_alone_configures_the_deployment_provider(self):
        self.eligible(self.context, "openai")
        paid = self.capabilities({"MARKETAI_PLANNING_PROVIDER": "openai", "MARKETAI_PLANNING_MODELS_JSON": TABLE})
        self.assertEqual((paid["available"], paid["provider"], paid["model"]), (True, "openai", "openai-text-test"))

    def test_the_deployment_provider_defaults_to_gemini(self):
        self.eligible(self.context, "openai")
        paid = self.capabilities({"MARKETAI_PLANNING_MODELS_JSON": json.dumps({
            "gemini": {"model": "gemini-text-test", "max_cost_usd": "0.20"},
            "openai": {"model": "openai-text-test", "max_cost_usd": "0.10"}})})
        self.assertEqual((paid["available"], paid["provider"]), (True, "openai"))

    def test_unusable_entries_are_not_configured(self):
        self.eligible(self.context, "openai")
        for table in ("{", "[]", json.dumps({"openai": "openai-text-test"}),
                      json.dumps({"openai": {"model": "openai-text-test", "max_cost_usd": "11"}}),
                      json.dumps({"openai": {"model": "openai-text-test", "max_cost_usd": "0"}}),
                      json.dumps({"openai": {"model": "openai-text-test", "max_cost_usd": "NaN"}}),
                      json.dumps({"openai": {"model": "openai-text-test", "max_cost_usd": 0.1}}),
                      json.dumps({"openai": {"model": "", "max_cost_usd": "0.10"}}),
                      json.dumps({"openai": {"model": 7, "max_cost_usd": "0.10"}})):
            with self.subTest(table=table):
                paid = self.capabilities({"MARKETAI_PLANNING_MODELS_JSON": table})
                self.assertEqual((paid["available"], paid["unavailable_reason"], paid["provider"], paid["maximum_cost"]),
                                 (False, "planner_not_configured", "gemini", "0"))

    def test_an_unsupported_provider_in_the_table_is_ignored(self):
        self.eligible(self.context, "openai")
        paid = self.capabilities({"MARKETAI_PLANNING_PROVIDER": "acme", "MARKETAI_PLANNING_MODELS_JSON": json.dumps({
            "acme": {"model": "acme-text", "max_cost_usd": "0.10"},
            "openai": {"model": "openai-text-test", "max_cost_usd": "0.10"}})})
        self.assertEqual((paid["available"], paid["provider"]), (True, "openai"))

    def test_a_table_entry_without_its_key_reports_that_provider(self):
        self.eligible(self.context)
        paid = self.capabilities({"MARKETAI_PLANNING_MODELS_JSON": TABLE})
        self.assertEqual((paid["available"], paid["unavailable_reason"], paid["provider"], paid["model"]),
                         (False, "credential_required", "openai", "openai-text-test"))

    def test_budget_and_entitlement_still_apply_to_the_fallback(self):
        self.eligible(self.context, "openai")
        paid = self.capabilities({**GEMINI_DEPLOYMENT, "MARKETAI_PLANNING_MODELS_JSON": json.dumps(
            {"openai": {"model": "openai-text-test", "max_cost_usd": "2"}})})
        self.assertEqual((paid["available"], paid["unavailable_reason"], paid["provider"]),
                         (False, "budget_exceeded", "openai"))

    def test_another_workspace_key_is_never_borrowed(self):
        self.eligible(self.context, "openai")
        other = self._make_context()
        self.eligible(other)
        env = {**GEMINI_DEPLOYMENT, "MARKETAI_PLANNING_MODELS_JSON": TABLE}
        self.assertTrue(self.capabilities(env)["available"])
        mine = self.context
        self.context = other
        try:
            paid = self.capabilities(env)
        finally:
            self.context = mine
        self.assertEqual((paid["available"], paid["unavailable_reason"], paid["provider"]),
                         (False, "credential_required", "gemini"))


class TestVisionCheckProviderChoice(ProviderChoiceFixtures):
    def test_vision_checks_fall_back_to_openai(self):
        self.eligible(self.context, "openai")
        self.assertEqual(self.qc({**QC_GEMINI_DEPLOYMENT, "MARKETAI_MARKETING_QC_MODELS_JSON": QC_TABLE}),
                         {"provider": "openai", "model": "vision-test", "unit_maximum": "0.10", "reason": None})

    def test_vision_checks_without_the_table_are_unchanged(self):
        self.eligible(self.context, "openai")
        self.assertEqual(self.qc(QC_GEMINI_DEPLOYMENT), {"provider": "gemini", "model": "gemini-vision-test",
                                                         "unit_maximum": "0", "reason": "credential_required"})

    def test_vision_checks_with_no_usable_entry_are_not_configured(self):
        self.eligible(self.context, "openai")
        self.assertEqual(self.qc({"MARKETAI_MARKETING_QC_MODELS_JSON": "{"})["reason"], "qc_not_configured")

    def test_vision_checks_read_their_own_table_not_planning(self):
        self.eligible(self.context, "openai")
        self.assertEqual(self.qc({**QC_GEMINI_DEPLOYMENT, "MARKETAI_PLANNING_MODELS_JSON": TABLE})["reason"],
                         "credential_required")

    def test_mock_rounds_never_read_the_settings(self):
        self.assertEqual(self.qc({"MARKETAI_MARKETING_QC_MODELS_JSON": "{"}, mode="mock")["provider"], "mock")
