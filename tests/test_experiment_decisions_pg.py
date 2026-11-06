"""PostgreSQL integration tests for Issue #22 immutable contextual decisions.

Covers §5.6 (FK, PU, AO, WO, RL, RG, SF, ER, DC, TC), §5.11.7 (SN),
§5.14.9 (ATI), and §5.5.3 PG-only (TD).

Prerequisite: the migration (t1a2b3c4d5e6_immutable_contextual_decisions) and role
scripts (deploy/postgres/domain-tenant-roles.sql) must already be applied
to the target PostgreSQL 16 instance.

Tests FAIL (not skip) when MARKETAI_PG_TEST_URL is absent in CI.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import unittest
import uuid
from contextlib import contextmanager

PG_URL = os.environ.get("MARKETAI_PG_TEST_URL", "")
IN_CI = os.environ.get("CI", "") != ""

if not PG_URL and IN_CI:
    raise RuntimeError(
        "MARKETAI_PG_TEST_URL is required in CI domain-rls job"
    )

try:
    import psycopg
    from psycopg import sql
    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False


def _skip_no_pg(cls):
    if not PG_URL or not HAS_PSYCOPG:
        return unittest.skip("MARKETAI_PG_TEST_URL not set or psycopg missing")(cls)
    return cls


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WS_A = "ws_pg_test_a"
WS_B = "ws_pg_test_b"
TEST_TENANT_SECRET = os.environ.get(
    "MARKETAI_TENANT_CONTEXT_SECRET",
    "test-hmac-secret-for-pg-integration-tests",
)

NEW_WORKSPACE_OWNED = (
    "experiment_versions", "experiment_version_candidates",
    "experiment_version_candidate_concepts", "experiment_version_concept_resolutions",
    "decision_proposals", "decision_proposal_state_events", "decision_confirmations",
    "experiment_eligibility_events", "experiment_eligibility_projections",
    "contextual_verdict_projections",
    "experiment_timeline_heads", "experiment_timeline_events",
    "experiment_notification_outbox", "experiment_next_round_records",
    "experiment_pattern_candidates",
)

APPEND_ONLY_TABLES = (
    "experiment_versions", "experiment_version_candidates",
    "experiment_version_candidate_concepts", "experiment_version_concept_resolutions",
    "decision_proposals", "decision_proposal_state_events", "decision_confirmations",
    "experiment_eligibility_events",
    "quality_reviews", "experiment_state_events", "judgements", "experiment_decisions",
    "asset_qc_check_results",
    "experiment_timeline_events", "experiment_notification_outbox",
    "experiment_next_round_records", "experiment_pattern_candidates",
)

IMMUTABLE_EVIDENCE_TABLES = (
    "experiment_versions", "experiment_version_candidates",
    "experiment_version_candidate_concepts", "experiment_version_concept_resolutions",
    "decision_proposals", "decision_proposal_state_events", "decision_confirmations",
    "experiment_eligibility_events",
    "experiment_timeline_events", "experiment_notification_outbox",
    "experiment_next_round_records", "experiment_pattern_candidates",
)

ADDENDUM_EVIDENCE_TABLES = (
    "experiment_notification_outbox",
    "experiment_next_round_records",
    "experiment_pattern_candidates",
    "experiment_timeline_events",
)

ADDENDUM_ALL_TABLES = ADDENDUM_EVIDENCE_TABLES + (
    "experiment_timeline_heads",
)

H64 = "a" * 64


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def _sign_context(workspace_id: str, epoch) -> str:
    """Compute HMAC matching marketai_context_is_current()."""
    msg = (
        f"marketai-tenant-context-v1\n{workspace_id}\n{epoch}"
    ).encode("utf-8")
    return hmac.new(
        TEST_TENANT_SECRET.encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()


def _set_local(conn, setting: str, value) -> None:
    """Set a transaction-local GUC through a parameter-safe SQL expression."""
    conn.execute("SELECT set_config(%s, %s, true)", (setting, str(value)))


def _public_can_execute(conn, function_signature: str) -> bool:
    """Return whether a function ACL grants EXECUTE to PostgreSQL PUBLIC."""
    return conn.execute(
        "SELECT COALESCE(bool_or(acl.grantee = 0 AND "
        "acl.privilege_type = 'EXECUTE'), false) "
        "FROM pg_proc p "
        "LEFT JOIN LATERAL aclexplode("
        "COALESCE(p.proacl, acldefault('f', p.proowner))) acl ON true "
        "WHERE p.oid = %s::regprocedure",
        (function_signature,),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Connection helpers — psycopg v3 sync
# ---------------------------------------------------------------------------

@contextmanager
def _pg_conn(role=None, workspace_id=None, epoch=None, autocommit=False):
    role_password_env = {
        "marketai_domain_owner": "DOMAIN_OWNER_PASSWORD",
        "marketai_domain_api": "DOMAIN_API_PASSWORD",
        "marketai_domain_worker": "DOMAIN_WORKER_PASSWORD",
        "marketai_domain_readonly": "DOMAIN_READONLY_PASSWORD",
    }
    password = os.environ.get(role_password_env.get(role, ""), "")
    ws_epoch = epoch
    if workspace_id and ws_epoch is None:
        with psycopg.connect(PG_URL) as lookup_conn:
            ws_epoch = _get_epoch(lookup_conn, workspace_id)

    if role and password:
        conn_params = psycopg.conninfo.conninfo_to_dict(PG_URL)
        conn_params.update(user=role, password=password)
        conn = psycopg.connect(**conn_params, autocommit=autocommit)
        set_role = False
    else:
        if role and IN_CI:
            raise RuntimeError(
                f"{role_password_env[role]} is required to test as {role} in CI"
            )
        conn = psycopg.connect(PG_URL, autocommit=autocommit)
        set_role = bool(role)
    try:
        if set_role:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        if workspace_id:
            sig = _sign_context(workspace_id, ws_epoch)
            _set_local(conn, "marketai.workspace_id", workspace_id)
            _set_local(conn, "marketai.authorization_epoch", ws_epoch)
            _set_local(conn, "marketai.context_signature", sig)
        yield conn
    finally:
        conn.close()


def _owner_conn(**kw):
    return _pg_conn(role="marketai_domain_owner", **kw)


def _api_conn(workspace_id=WS_A, **kw):
    return _pg_conn(role="marketai_domain_api", workspace_id=workspace_id, **kw)


def _worker_conn(workspace_id=WS_A, **kw):
    return _pg_conn(role="marketai_domain_worker", workspace_id=workspace_id, **kw)


def _readonly_conn(workspace_id=WS_A, **kw):
    return _pg_conn(role="marketai_domain_readonly", workspace_id=workspace_id, **kw)


def _get_epoch(conn, workspace_id):
    row = conn.execute(
        "SELECT authorization_epoch FROM workspaces WHERE id = %s",
        (workspace_id,),
    ).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def _seed_workspace(conn, ws_id):
    conn.execute(
        "INSERT INTO workspaces "
        "(id, name, default_currency, lifecycle, budget_currency, created_at) "
        "VALUES (%s, %s, 'USD', 'active', 'USD', NOW()) "
        "ON CONFLICT DO NOTHING",
        (ws_id, f"Test WS {ws_id}"),
    )


def _seed_product(conn, ws_id, prod_id):
    conn.execute(
        "INSERT INTO products "
        "(id, workspace_id, name, description, selling_points, audience, "
        "brand_notes, ref_images, economics, revision, created_at, updated_at) "
        "VALUES (%s, %s, %s, '', '[]', '', '', '[]', '{}', 1, NOW(), NOW()) "
        "ON CONFLICT DO NOTHING",
        (prod_id, ws_id, "Test product"),
    )


def _seed_library_entry_and_version(conn, ws_id, entry_id, ver_id):
    conn.execute(
        "INSERT INTO library_entries "
        "(id, workspace_id, entry_type, display_name, lifecycle, created_at, updated_at) "
        "VALUES (%s, %s, 'product', 'test', 'active', NOW(), NOW()) "
        "ON CONFLICT DO NOTHING",
        (entry_id, ws_id),
    )
    conn.execute(
        "INSERT INTO library_versions "
        "(id, workspace_id, entry_id, entry_type, version_no, content_json, "
        "content_hash, content_schema_version, lifecycle_revision, state, "
        "eligibility, validation_json, origin_command, published_by, "
        "published_at, withdrawn_by, withdraw_reason) "
        "VALUES (%s, %s, %s, 'product', 1, '{}', %s, 1, 1, 'effective', "
        "'eligible', '{}', 'create', 'pg-test', NOW(), '', '') "
        "ON CONFLICT DO NOTHING",
        (ver_id, ws_id, entry_id, H64),
    )


def _seed_experiment(conn, ws_id, exp_id, prod_id,
                     schema_version=2, state="frozen"):
    conn.execute(
        "INSERT INTO experiments (id, workspace_id, product_id, name, state, "
        "revision, config_json, metric_contract_hash, record_schema_version, "
        "created_at) "
        "VALUES (%s,%s,%s,'Test',%s,1,'{}',%s,%s,NOW()) "
        "ON CONFLICT DO NOTHING",
        (exp_id, ws_id, prod_id, state, H64, schema_version),
    )


def _seed_experiment_version(conn, ws_id, exp_id, ver_id, pv_id, ov_id):
    conn.execute(
        "INSERT INTO experiment_versions "
        "(id, workspace_id, experiment_id, canonicalization_version, "
        "frozen_snapshot_json, frozen_snapshot_hash, metric_contract_hash, "
        "market, placement, landing_page_evidence_hash, "
        "audience_snapshot_json, audience_snapshot_hash, "
        "daily_budget_amount, total_budget_cap, budget_currency, "
        "schedule_start_at, schedule_end_at, schedule_timezone, "
        "attribution_click_window_days, attribution_view_window_days, "
        "statistical_plan_json, statistical_plan_hash, "
        "marketing_vars_schema_version, product_version_id, offer_version_id, "
        "candidates_hash, concept_resolutions_hash, created_at) "
        "VALUES (%s,%s,%s,'p1-migration-v1',"
        "'{}',%s,%s,"
        "'US','feed',%s,"
        "'{}',%s,"
        "100,3000,'USD',"
        "'2026-01-01T00:00:00+00','2026-12-31T23:59:59+00','UTC',"
        "7,1,'{}',%s,'1',%s,%s,%s,%s,NOW()) "
        "ON CONFLICT DO NOTHING",
        (ver_id, ws_id, exp_id,
         H64, H64, H64, H64, H64, pv_id, ov_id, H64, H64),
    )


def _seed_asset_version(conn, ws_id, prod_id, av_id):
    plan_id = f"plan_{av_id}"
    run_id = f"run_{av_id}"
    asset_id = f"asset_{av_id}"
    conn.execute(
        "INSERT INTO plans "
        "(id, workspace_id, product_id, mode, cap, total_full, snapshot_hash, created_at) "
        "VALUES (%s, %s, %s, 'full', 0, 1, %s, NOW()) "
        "ON CONFLICT DO NOTHING",
        (plan_id, ws_id, prod_id, H64),
    )
    conn.execute(
        "INSERT INTO generation_runs "
        "(id, workspace_id, product_id, plan_id, provider, model, aspect_ratio, "
        "image_size, variants, use_ref, status, total, done, failed, est_cost, "
        "currency, revision, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, 'mock', 'mock', '1:1', '1024', 1, false, "
        "'done', 1, 1, 0, 0, 'USD', 1, NOW(), NOW()) "
        "ON CONFLICT DO NOTHING",
        (run_id, ws_id, prod_id, plan_id),
    )
    conn.execute(
        "INSERT INTO assets "
        "(id, workspace_id, product_id, run_id, combo_id, combo_code, tags_json, "
        "filename, provider, model, prompt, fingerprint, status, error_code, "
        "error_detail, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, 'C01', 'A1', '{}', 'test.png', 'mock', "
        "'mock', 'test', %s, 'ok', '', '', NOW(), NOW()) "
        "ON CONFLICT DO NOTHING",
        (asset_id, ws_id, prod_id, run_id, f"fp_{av_id}"),
    )
    conn.execute(
        "INSERT INTO asset_versions "
        "(id, workspace_id, asset_id, kind, aspect_ratio, language, object_key, "
        "content_sha256, byte_size, mime_type, origin_kind, execution_mode, "
        "truth_dependency_expected, truth_dependency_policy_version, "
        "truth_dependency_evidence_json, truth_dependency_evidence_hash, created_at) "
        "VALUES (%s, %s, %s, 'generated', '1:1', 'en', %s, %s, 1, "
        "'image/png', 'generation', 'mock', false, 'v2-test', '{}', %s, NOW()) "
        "ON CONFLICT DO NOTHING",
        (av_id, ws_id, asset_id, f"test/{av_id}.png", H64, H64),
    )


def _seed_candidate(conn, ws_id, ver_id, cand_id, prod_id, av_id="", pos=0):
    if not av_id:
        av_id = _uid("av_")
    _seed_asset_version(conn, ws_id, prod_id, av_id)
    conn.execute(
        "INSERT INTO experiment_version_candidates "
        "(id, workspace_id, experiment_version_id, source_kind, "
        "asset_version_id, asset_version_content_sha256, position, "
        "marketing_vars_json, marketing_vars_hash, execution_mode, "
        "eligibility_evidence_json, eligibility_evidence_hash, "
        "concepts_hash, candidate_hash, evaluated_at, created_at) "
        "VALUES (%s,%s,%s,'generation',"
        "%s,%s,%s,'{}',%s,'mock','{}',%s,%s,%s,NOW(),NOW()) "
        "ON CONFLICT DO NOTHING",
        (cand_id, ws_id, ver_id, av_id, H64, pos, H64, H64, H64, H64),
    )


def _seed_judgement(conn, ws_id, exp_id, jud_id,
                    ver_id=None, schema_version=None):
    conn.execute(
        "INSERT INTO judgements "
        "(id, workspace_id, experiment_id, experiment_version_id, "
        "algorithm_version, as_of, snapshot_hash, "
        "input_snapshot_json, result_json, record_schema_version, created_at) "
        "VALUES (%s,%s,%s,%s,'judge-v1',CURRENT_DATE,%s,'{}',%s,%s,NOW()) "
        "ON CONFLICT DO NOTHING",
        (jud_id, ws_id, exp_id, ver_id, _uid("sh_"),
         '{"entries":[]}', schema_version),
    )


def _seed_proposal(conn, ws_id, exp_id, ver_id, jud_id, prop_id):
    conn.execute(
        "INSERT INTO decision_proposals "
        "(id, workspace_id, experiment_id, judgement_id, experiment_version_id, "
        "eligibility_context_hash, proposal_hash, "
        "proposed_entries_json, evidence_json, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'[]','{}',NOW()) "
        "ON CONFLICT DO NOTHING",
        (prop_id, ws_id, exp_id, jud_id, ver_id, _uid("ech_"), _uid("ph_")),
    )


def _seed_proposal_state_event(conn, ws_id, prop_id, event_id,
                                seq=1, event_type="created"):
    conn.execute(
        "INSERT INTO decision_proposal_state_events "
        "(id, workspace_id, proposal_id, event_type, sequence, created_at) "
        "VALUES (%s,%s,%s,%s,%s,NOW()) ON CONFLICT DO NOTHING",
        (event_id, ws_id, prop_id, event_type, seq),
    )


def _seed_confirmation(conn, ws_id, prop_id, conf_id):
    conn.execute(
        "INSERT INTO decision_confirmations "
        "(id, workspace_id, proposal_id, proposal_hash, "
        "expected_proposal_sequence, idempotency_key_hash, "
        "request_fingerprint_hash, actor_id, role, "
        "authorization_epoch, authorization_evidence_json, "
        "confirmed_at, created_at) "
        "VALUES (%s,%s,%s,%s,1,%s,%s,'user1','owner',1,'{}',NOW(),NOW()) "
        "ON CONFLICT DO NOTHING",
        (conf_id, ws_id, prop_id, _uid("ph_"), _uid("ik_"), _uid("rf_")),
    )


def _seed_full_lineage(conn, ws_id, tag):
    """Seed a complete FK lineage and return IDs dict."""
    prod_id = f"prod_{tag}"
    exp_id = f"exp_{tag}"
    le_id = f"le_{tag}"
    pv_id = f"lv_pv_{tag}"
    ov_id = f"lv_ov_{tag}"
    ver_id = f"ver_{tag}"
    cand_id = f"cand_{tag}"
    jud_id = f"jud_{tag}"
    prop_id = f"prop_{tag}"
    conf_id = f"conf_{tag}"

    _seed_workspace(conn, ws_id)
    _seed_product(conn, ws_id, prod_id)
    _seed_library_entry_and_version(conn, ws_id, le_id, pv_id)
    _seed_library_entry_and_version(conn, ws_id, f"le_ov_{tag}", ov_id)
    _seed_experiment(conn, ws_id, exp_id, prod_id)
    _seed_experiment_version(conn, ws_id, exp_id, ver_id, pv_id, ov_id)
    _seed_candidate(conn, ws_id, ver_id, cand_id, prod_id, f"av_{tag}")
    _seed_judgement(conn, ws_id, exp_id, jud_id, ver_id, 2)
    _seed_proposal(conn, ws_id, exp_id, ver_id, jud_id, prop_id)
    _seed_proposal_state_event(conn, ws_id, prop_id, f"dpse_{tag}")
    _seed_confirmation(conn, ws_id, prop_id, conf_id)

    return {
        "prod_id": prod_id, "exp_id": exp_id, "ver_id": ver_id,
        "cand_id": cand_id, "jud_id": jud_id, "prop_id": prop_id,
        "conf_id": conf_id, "pv_id": pv_id, "ov_id": ov_id,
    }


def _ensure_test_secret(conn):
    """Ensure tenant_context_secrets has our test secret."""
    conn.execute(
        "INSERT INTO tenant_context_secrets (key_id, secret) "
        "VALUES (1, %s) ON CONFLICT (key_id) DO UPDATE SET secret = EXCLUDED.secret",
        (TEST_TENANT_SECRET,),
    )


# ===================================================================
# Module-level setup: seed tenant secret + base workspaces
# ===================================================================

def _module_setup():
    if not PG_URL or not HAS_PSYCOPG:
        return
    with _owner_conn() as conn:
        _ensure_test_secret(conn)
        _seed_workspace(conn, WS_A)
        _seed_workspace(conn, WS_B)
        conn.commit()


_module_setup()


# ===================================================================
# §5.6.1 — Composite FK enforcement (FK-01..FK-10)
# ===================================================================

@_skip_no_pg
class TestFK(unittest.TestCase):
    """Composite FK enforcement."""

    @classmethod
    def setUpClass(cls):
        with _owner_conn() as conn:
            cls.ids = _seed_full_lineage(conn, WS_A, "fk")
            _seed_product(conn, WS_B, "prod_fk_b")
            _seed_asset_version(conn, WS_B, "prod_fk_b", "av_fk_b")
            conn.commit()

    def _assert_fk_violation(self, dml, params):
        with _owner_conn() as conn:
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                conn.execute(dml, params)

    def test_FK01_cross_workspace_fk_insert(self):
        """FK-01: Child workspace_id != parent's."""
        self._assert_fk_violation(
            "INSERT INTO experiment_version_candidates "
            "(id,workspace_id,experiment_version_id,source_kind,"
            "asset_version_id,asset_version_content_sha256,position,"
            "marketing_vars_json,marketing_vars_hash,execution_mode,"
            "eligibility_evidence_json,eligibility_evidence_hash,"
            "concepts_hash,candidate_hash,evaluated_at,created_at) "
            "VALUES (%s,%s,%s,'generation','av_fk_b',%s,999,'{}',%s,"
            "'mock','{}',%s,%s,%s,NOW(),NOW())",
            (_uid("cand_"), WS_B, self.ids["ver_id"], H64, H64,
             H64, H64, H64),
        )

    def test_FK02_eligibility_event_mismatched_version(self):
        """FK-02: 3-col FK to experiment_versions."""
        self._assert_fk_violation(
            "INSERT INTO experiment_eligibility_events "
            "(id,workspace_id,experiment_id,experiment_version_id,"
            "upstream_event_type,upstream_resource_type,upstream_resource_id,"
            "upstream_event_id,previous_effective_eligibility,"
            "current_effective_eligibility,reversible,evidence_json,"
            "observed_at,created_at) "
            "VALUES (%s,%s,%s,%s,"
            "'review_rejected','quality_review',%s,%s,"
            "'eligible','ineligible',true,'{}',NOW(),NOW())",
            (_uid("eee_"), WS_A, self.ids["exp_id"], _uid("wrong_ver_"),
             _uid(), _uid()),
        )

    def test_FK03_proposal_judgement_wrong_experiment(self):
        """FK-03: 4-col FK (w,exp,ev,jid) to judgements."""
        wrong_exp = _uid("wrongexp_")
        with _owner_conn() as conn:
            _seed_experiment(conn, WS_A, wrong_exp, self.ids["prod_id"])
            conn.commit()
        self._assert_fk_violation(
            "INSERT INTO decision_proposals "
            "(id,workspace_id,experiment_id,judgement_id,experiment_version_id,"
            "eligibility_context_hash,proposal_hash,"
            "proposed_entries_json,evidence_json,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'[]','{}',NOW())",
            (_uid("dp_"), WS_A, wrong_exp, self.ids["jud_id"],
             self.ids["ver_id"], _uid(), _uid()),
        )

    def test_FK04_proposal_judgement_wrong_version(self):
        """FK-04: Same 4-col FK — wrong version."""
        wrong_ver = _uid("wrongver_")
        with _owner_conn() as conn:
            le2 = _uid("le2_")
            pv2 = _uid("lv2_")
            ov2 = _uid("ov2_")
            _seed_library_entry_and_version(conn, WS_A, le2, pv2)
            _seed_library_entry_and_version(conn, WS_A, _uid("le_ov2_"), ov2)
            _seed_experiment_version(
                conn, WS_A, self.ids["exp_id"], wrong_ver, pv2, ov2)
            conn.commit()
        self._assert_fk_violation(
            "INSERT INTO decision_proposals "
            "(id,workspace_id,experiment_id,judgement_id,experiment_version_id,"
            "eligibility_context_hash,proposal_hash,"
            "proposed_entries_json,evidence_json,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'[]','{}',NOW())",
            (_uid("dp_"), WS_A, self.ids["exp_id"], self.ids["jud_id"],
             wrong_ver, _uid(), _uid()),
        )

    def test_FK05_proposal_version_wrong_experiment(self):
        """FK-05: 3-col FK (w,exp,ev) to experiment_versions."""
        wrong_exp = _uid("wrongexp5_")
        with _owner_conn() as conn:
            _seed_experiment(conn, WS_A, wrong_exp, self.ids["prod_id"])
            conn.commit()
        self._assert_fk_violation(
            "INSERT INTO decision_proposals "
            "(id,workspace_id,experiment_id,judgement_id,experiment_version_id,"
            "eligibility_context_hash,proposal_hash,"
            "proposed_entries_json,evidence_json,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'[]','{}',NOW())",
            (_uid("dp_"), WS_A, wrong_exp, self.ids["jud_id"],
             self.ids["ver_id"], _uid(), _uid()),
        )

    def test_FK06_decision_candidate_wrong_version(self):
        """FK-06: 3-col FK (w,ev,cand) to candidates."""
        wrong_cand = _uid("wrongcand_")
        self._assert_fk_violation(
            "INSERT INTO experiment_decisions "
            "(id,workspace_id,experiment_id,judgement_id,experiment_entry_id,"
            "experiment_version_id,experiment_version_candidate_id,"
            "proposal_id,confirmation_id,"
            "verdict,metric,reason_codes_json,"
            "actor_id,confirmed_at,context_hash,decision_schema_version,"
            "created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "'win','ctr','[]','user1',NOW(),%s,2,NOW())",
            (_uid("ed_"), WS_A, self.ids["exp_id"], self.ids["jud_id"],
             _uid("entry_"),
             self.ids["ver_id"], wrong_cand,
             self.ids["prop_id"], self.ids["conf_id"], _uid()),
        )

    def test_FK07_decision_confirmation_wrong_proposal(self):
        """FK-07: 3-col FK (w,conf,prop) to confirmations."""
        with _owner_conn() as conn:
            wrong_prop = _uid("wrongprop_")
            wrong_conf = _uid("wrongconf_")
            _seed_proposal(conn, WS_A, self.ids["exp_id"],
                           self.ids["ver_id"], self.ids["jud_id"], wrong_prop)
            _seed_proposal_state_event(conn, WS_A, wrong_prop, _uid("dpse_"))
            _seed_confirmation(conn, WS_A, wrong_prop, wrong_conf)
            conn.commit()
        self._assert_fk_violation(
            "INSERT INTO experiment_decisions "
            "(id,workspace_id,experiment_id,judgement_id,experiment_entry_id,"
            "experiment_version_id,experiment_version_candidate_id,"
            "proposal_id,confirmation_id,"
            "verdict,metric,reason_codes_json,"
            "actor_id,confirmed_at,context_hash,decision_schema_version,"
            "created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "'win','ctr','[]','user1',NOW(),%s,2,NOW())",
            (_uid("ed_"), WS_A, self.ids["exp_id"], self.ids["jud_id"],
             _uid("entry_"),
             self.ids["ver_id"], self.ids["cand_id"],
             self.ids["prop_id"], wrong_conf, _uid()),
        )

    def test_FK08_decision_experiment_mismatch_proposal(self):
        """FK-08: 5-col FK to proposals — experiment mismatch."""
        wrong_exp = _uid("wrongexp8_")
        with _owner_conn() as conn:
            _seed_experiment(conn, WS_A, wrong_exp, self.ids["prod_id"])
            conn.commit()
        self._assert_fk_violation(
            "INSERT INTO experiment_decisions "
            "(id,workspace_id,experiment_id,judgement_id,experiment_entry_id,"
            "experiment_version_id,experiment_version_candidate_id,"
            "proposal_id,confirmation_id,"
            "verdict,metric,reason_codes_json,"
            "actor_id,confirmed_at,context_hash,decision_schema_version,"
            "created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "'win','ctr','[]','user1',NOW(),%s,2,NOW())",
            (_uid("ed_"), WS_A, wrong_exp, self.ids["jud_id"],
             _uid("entry_"),
             self.ids["ver_id"], self.ids["cand_id"],
             self.ids["prop_id"], self.ids["conf_id"], _uid()),
        )

    def test_FK09_decision_version_mismatch_proposal(self):
        """FK-09: 5-col FK — version mismatch."""
        wrong_ver = _uid("wrongver9_")
        self._assert_fk_violation(
            "INSERT INTO experiment_decisions "
            "(id,workspace_id,experiment_id,judgement_id,experiment_entry_id,"
            "experiment_version_id,experiment_version_candidate_id,"
            "proposal_id,confirmation_id,"
            "verdict,metric,reason_codes_json,"
            "actor_id,confirmed_at,context_hash,decision_schema_version,"
            "created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "'win','ctr','[]','user1',NOW(),%s,2,NOW())",
            (_uid("ed_"), WS_A, self.ids["exp_id"], self.ids["jud_id"],
             _uid("entry_"),
             wrong_ver, self.ids["cand_id"],
             self.ids["prop_id"], self.ids["conf_id"], _uid()),
        )

    def test_FK10_decision_judgement_mismatch_proposal(self):
        """FK-10: 5-col FK — judgement mismatch."""
        wrong_jud = _uid("wrongjud_")
        self._assert_fk_violation(
            "INSERT INTO experiment_decisions "
            "(id,workspace_id,experiment_id,judgement_id,experiment_entry_id,"
            "experiment_version_id,experiment_version_candidate_id,"
            "proposal_id,confirmation_id,"
            "verdict,metric,reason_codes_json,"
            "actor_id,confirmed_at,context_hash,decision_schema_version,"
            "created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "'win','ctr','[]','user1',NOW(),%s,2,NOW())",
            (_uid("ed_"), WS_A, self.ids["exp_id"], wrong_jud,
             _uid("entry_"),
             self.ids["ver_id"], self.ids["cand_id"],
             self.ids["prop_id"], self.ids["conf_id"], _uid()),
        )


# ===================================================================
# §5.6.2 — Partial uniques and CHECKs (PU-01..PU-08)
# ===================================================================

@_skip_no_pg
class TestPU(unittest.TestCase):
    """Partial unique and CHECK constraint enforcement."""

    @classmethod
    def setUpClass(cls):
        with _owner_conn() as conn:
            cls.ids = _seed_full_lineage(conn, WS_A, "pu")
            conn.commit()

    def test_PU01_source_kind_check(self):
        """PU-01: source_kind CHECK on candidates."""
        with _owner_conn() as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO experiment_version_candidates "
                    "(id,workspace_id,experiment_version_id,source_kind,"
                    "asset_version_id,asset_version_content_sha256,position,"
                    "marketing_vars_json,marketing_vars_hash,execution_mode,"
                    "eligibility_evidence_json,eligibility_evidence_hash,"
                    "concepts_hash,candidate_hash,evaluated_at,created_at) "
                    "VALUES (%s,%s,%s,'INVALID_KIND',%s,%s,99,"
                    "'{}',%s,'mock','{}',%s,%s,%s,NOW(),NOW())",
                    (_uid("c_"), WS_A, self.ids["ver_id"],
                     _uid("av_"), H64, H64, H64, H64, H64),
                )

    def test_PU02_execution_mode_check(self):
        """PU-02: execution_mode CHECK on candidates."""
        with _owner_conn() as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO experiment_version_candidates "
                    "(id,workspace_id,experiment_version_id,source_kind,"
                    "asset_version_id,asset_version_content_sha256,position,"
                    "marketing_vars_json,marketing_vars_hash,execution_mode,"
                    "eligibility_evidence_json,eligibility_evidence_hash,"
                    "concepts_hash,candidate_hash,evaluated_at,created_at) "
                    "VALUES (%s,%s,%s,'generation',%s,%s,99,"
                    "'{}',%s,'invalid_mode','{}',%s,%s,%s,NOW(),NOW())",
                    (_uid("c_"), WS_A, self.ids["ver_id"],
                     _uid("av_"), H64, H64, H64, H64, H64),
                )

    def test_PU03_verdict_projection_check(self):
        """PU-03: verdict CHECK on contextual_verdict_projections."""
        with _owner_conn() as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO contextual_verdict_projections "
                    "(id,workspace_id,experiment_id,experiment_version_id,"
                    "candidate_id,decision_id,confirmation_id,"
                    "verdict,metric,asset_version_id,"
                    "product_version_id,offer_version_id,"
                    "market,placement,landing_page_evidence_hash,"
                    "confirmed_at,updated_at,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,"
                    "'inconclusive','ctr',%s,%s,%s,'US','feed',%s,"
                    "NOW(),NOW(),NOW())",
                    (_uid("cvp_"), WS_A, self.ids["exp_id"],
                     self.ids["ver_id"], self.ids["cand_id"],
                     _uid("d_"), self.ids["conf_id"],
                     _uid("av_"), self.ids["pv_id"], self.ids["ov_id"], H64),
                )

    def test_PU04_eligibility_level_check(self):
        """PU-04: level CHECK on eligibility projections."""
        with _owner_conn() as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO experiment_eligibility_projections "
                    "(id,workspace_id,experiment_id,experiment_version_id,"
                    "level,current_eligibility,irreversible_active,"
                    "head_hash,head_sequence,updated_at) "
                    "VALUES (%s,%s,%s,%s,'invalid','eligible',false,%s,0,NOW())",
                    (_uid("eep_"), WS_A, self.ids["exp_id"],
                     self.ids["ver_id"], H64),
                )

    def test_PU05_proposal_state_event_type_check(self):
        """PU-05: event_type CHECK on proposal state events."""
        with _owner_conn() as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO decision_proposal_state_events "
                    "(id,workspace_id,proposal_id,event_type,sequence,created_at) "
                    "VALUES (%s,%s,%s,'invalid_type',99,NOW())",
                    (_uid("dpse_"), WS_A, self.ids["prop_id"]),
                )

    def test_PU06_record_schema_version_check(self):
        """PU-06: record_schema_version CHECK — value=3 rejected."""
        with _owner_conn() as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO experiments "
                    "(id,workspace_id,product_id,name,state,revision,"
                    "config_json,metric_contract_hash,record_schema_version,"
                    "created_at) "
                    "VALUES (%s,%s,%s,'T','draft',1,'{}',%s,3,NOW())",
                    (_uid("exp_"), WS_A, self.ids["prod_id"], H64),
                )

    def test_PU07_product_profile_partial_unique(self):
        """PU-07: Duplicate product_profile_entry_id in same workspace."""
        ppid = _uid("pp_")
        with _owner_conn() as conn:
            p1, p2 = _uid("p1_"), _uid("p2_")
            _seed_product(conn, WS_A, p1)
            conn.execute(
                "UPDATE products SET product_profile_entry_id=%s WHERE id=%s",
                (ppid, p1),
            )
            _seed_product(conn, WS_A, p2)
            with self.assertRaises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "UPDATE products SET product_profile_entry_id=%s WHERE id=%s",
                    (ppid, p2),
                )

    def test_PU08_pattern_candidate_verdict_check(self):
        """PU-08: verdict CHECK on pattern candidates."""
        with _owner_conn() as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO experiment_pattern_candidates "
                    "(id,workspace_id,experiment_id,experiment_version_id,"
                    "decision_id,candidate_id,"
                    "source_concepts_hash,concept_count,execution_mode,"
                    "verdict,pattern_evidence_json,pattern_evidence_hash,"
                    "actor_id,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,1,'mock',"
                    "'inconclusive','{}', %s,'user1',NOW())",
                    (_uid("epc_"), WS_A, self.ids["exp_id"],
                     self.ids["ver_id"], _uid("d_"), self.ids["cand_id"],
                     H64, H64),
                )


# ===================================================================
# §5.6.3 — Append-only trigger enforcement (AO)
# ===================================================================

@_skip_no_pg
class TestAO(unittest.TestCase):
    """Append-only trigger enforcement."""

    @classmethod
    def setUpClass(cls):
        with _owner_conn() as conn:
            cls.ids = _seed_full_lineage(conn, WS_A, "ao")
            conn.execute(
                "INSERT INTO experiment_state_events "
                "(id,workspace_id,experiment_id,from_state,to_state,revision,"
                "actor_id,reason,created_at) "
                "VALUES (%s,%s,%s,'draft','frozen',1,'user','created',NOW()) "
                "ON CONFLICT DO NOTHING",
                ("ese_ao", WS_A, cls.ids["exp_id"]),
            )
            conn.execute(
                "INSERT INTO experiment_timeline_heads "
                "(id,workspace_id,experiment_id,head_sequence) "
                "VALUES (%s,%s,%s,1) ON CONFLICT DO NOTHING",
                ("eth_ao", WS_A, cls.ids["exp_id"]),
            )
            te_id = "ete_ao"
            conn.execute(
                "INSERT INTO experiment_timeline_events "
                "(id,workspace_id,experiment_id,timeline_sequence,"
                "event_type,source_resource_type,source_resource_id,"
                "summary_json,observed_at,created_at) "
                "VALUES (%s,%s,%s,1,'frozen','experiment',%s,"
                "'{}',NOW(),NOW()) ON CONFLICT DO NOTHING",
                (te_id, WS_A, cls.ids["exp_id"], cls.ids["exp_id"]),
            )
            te_id = conn.execute(
                "SELECT id FROM experiment_timeline_events "
                "WHERE workspace_id=%s AND experiment_id=%s "
                "AND timeline_sequence=1",
                (WS_A, cls.ids["exp_id"]),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO experiment_notification_outbox "
                "(id,workspace_id,experiment_id,timeline_event_id,"
                "notification_type,payload_canonical_json,payload_hash,"
                "payload_schema_version,created_at) "
                "VALUES (%s,%s,%s,%s,'frozen','{}', %s,1,NOW()) "
                "ON CONFLICT DO NOTHING",
                ("eno_ao", WS_A, cls.ids["exp_id"], te_id, H64),
            )
            conn.execute(
                "INSERT INTO experiment_eligibility_projections "
                "(id,workspace_id,experiment_id,experiment_version_id,"
                "level,current_eligibility,irreversible_active,"
                "head_hash,head_sequence,updated_at) "
                "VALUES (%s,%s,%s,%s,'version','eligible',false,%s,0,NOW()) "
                "ON CONFLICT DO NOTHING",
                ("eep_ao", WS_A, cls.ids["exp_id"],
                 cls.ids["ver_id"], H64),
            )
            conn.commit()

    def test_AO_update_rejected(self):
        """AO: UPDATE on each append-only table is trigger-rejected."""
        for table in APPEND_ONLY_TABLES:
            with self.subTest(table=table):
                with _owner_conn() as conn:
                    row = conn.execute(
                        sql.SQL("SELECT * FROM {} LIMIT 1").format(
                            sql.Identifier(table))
                    ).fetchone()
                    if row is None:
                        continue
                    pk_col = conn.execute(
                        "SELECT a.attname FROM pg_index i "
                        "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                        "AND a.attnum = ANY(i.indkey) "
                        "WHERE i.indrelid = %s::regclass AND i.indisprimary",
                        (table,),
                    ).fetchone()[0]
                    pk_val = row[0]
                    with self.assertRaises(psycopg.errors.RaiseException):
                        conn.execute(
                            sql.SQL(
                                "UPDATE {} SET created_at = NOW() "
                                "WHERE {} = %s"
                            ).format(
                                sql.Identifier(table),
                                sql.Identifier(pk_col),
                            ),
                            (pk_val,),
                        )

    def test_AO_delete_rejected(self):
        """AO: DELETE (non-erasure) on each append-only table rejected."""
        for table in APPEND_ONLY_TABLES:
            with self.subTest(table=table):
                with _owner_conn() as conn:
                    row = conn.execute(
                        sql.SQL("SELECT * FROM {} LIMIT 1").format(
                            sql.Identifier(table))
                    ).fetchone()
                    if row is None:
                        continue
                    pk_col = conn.execute(
                        "SELECT a.attname FROM pg_index i "
                        "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                        "AND a.attnum = ANY(i.indkey) "
                        "WHERE i.indrelid = %s::regclass AND i.indisprimary",
                        (table,),
                    ).fetchone()[0]
                    pk_val = row[0]
                    with self.assertRaises(psycopg.errors.RaiseException):
                        conn.execute(
                            sql.SQL(
                                "DELETE FROM {} WHERE {} = %s"
                            ).format(
                                sql.Identifier(table),
                                sql.Identifier(pk_col),
                            ),
                            (pk_val,),
                        )

    def test_AO_M1_eligibility_projections_update_allowed(self):
        """AO-M1: eligibility projections UPDATE allowed."""
        with _owner_conn() as conn:
            result = conn.execute(
                "UPDATE experiment_eligibility_projections "
                "SET head_sequence = head_sequence + 1 "
                "WHERE workspace_id = %s AND experiment_id = %s",
                (WS_A, self.ids["exp_id"]),
            )
            self.assertGreaterEqual(result.rowcount, 0)
            conn.rollback()

    def test_AO_M2_eligibility_projections_delete_rejected(self):
        """AO-M2: eligibility projections DELETE (non-erasure) rejected."""
        with _owner_conn() as conn:
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "DELETE FROM experiment_eligibility_projections "
                    "WHERE workspace_id = %s AND experiment_id = %s",
                    (WS_A, self.ids["exp_id"]),
                )


# ===================================================================
# §5.6.4 — Column write-once trigger enforcement (WO-01..WO-12)
# ===================================================================

@_skip_no_pg
class TestWO(unittest.TestCase):
    """Column write-once trigger enforcement."""

    @classmethod
    def setUpClass(cls):
        with _owner_conn() as conn:
            cls.ids = _seed_full_lineage(conn, WS_A, "wo")
            conn.commit()

    def test_WO01_product_profile_null_to_value_allowed(self):
        pid = _uid("p_wo1_")
        with _owner_conn() as conn:
            _seed_product(conn, WS_A, pid)
            conn.execute(
                "UPDATE products SET product_profile_entry_id=%s WHERE id=%s",
                (_uid("pp_"), pid))
            conn.rollback()

    def test_WO02_product_profile_value_to_different_rejected(self):
        pid = _uid("p_wo2_")
        with _owner_conn() as conn:
            _seed_product(conn, WS_A, pid)
            conn.execute(
                "UPDATE products SET product_profile_entry_id=%s WHERE id=%s",
                ("first_val", pid))
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "UPDATE products SET product_profile_entry_id=%s "
                    "WHERE id=%s", ("second_val", pid))

    def test_WO03_product_profile_value_to_null_rejected(self):
        pid = _uid("p_wo3_")
        with _owner_conn() as conn:
            _seed_product(conn, WS_A, pid)
            conn.execute(
                "UPDATE products SET product_profile_entry_id=%s WHERE id=%s",
                ("set_val", pid))
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "UPDATE products SET product_profile_entry_id=NULL "
                    "WHERE id=%s", (pid,))

    def test_WO04_experiments_schema_version_null_to_2_rejected(self):
        eid = _uid("e_wo4_")
        with _owner_conn() as conn:
            _seed_experiment(conn, WS_A, eid, self.ids["prod_id"],
                            schema_version=None, state="draft")
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "UPDATE experiments SET record_schema_version=2 "
                    "WHERE id=%s", (eid,))

    def test_WO05_experiments_schema_version_2_to_null_rejected(self):
        eid = _uid("e_wo5_")
        with _owner_conn() as conn:
            _seed_experiment(conn, WS_A, eid, self.ids["prod_id"],
                            schema_version=2, state="draft")
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "UPDATE experiments SET record_schema_version=NULL "
                    "WHERE id=%s", (eid,))

    def test_WO06_experiments_schema_version_same_allowed(self):
        eid = _uid("e_wo6_")
        with _owner_conn() as conn:
            _seed_experiment(conn, WS_A, eid, self.ids["prod_id"],
                            schema_version=2, state="draft")
            conn.execute(
                "UPDATE experiments SET record_schema_version=2 "
                "WHERE id=%s", (eid,))
            conn.rollback()

    def test_WO07_experiments_started_at_null_to_value_allowed(self):
        eid = _uid("e_wo7_")
        with _owner_conn() as conn:
            _seed_experiment(conn, WS_A, eid, self.ids["prod_id"],
                            schema_version=2, state="frozen")
            conn.execute(
                "UPDATE experiments SET started_at=NOW() WHERE id=%s", (eid,))
            conn.rollback()

    def test_WO08_experiments_started_at_value_to_different_rejected(self):
        eid = _uid("e_wo8_")
        with _owner_conn() as conn:
            _seed_experiment(conn, WS_A, eid, self.ids["prod_id"],
                            schema_version=2, state="running")
            conn.execute(
                "UPDATE experiments SET started_at=NOW() WHERE id=%s", (eid,))
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "UPDATE experiments SET "
                    "started_at=NOW() + interval '1 hour' WHERE id=%s",
                    (eid,))

    def test_WO09_ad_bindings_schema_null_to_2_rejected(self):
        entry_id = _uid("entry_wo9_")
        binding_id = _uid("bind_wo9_")
        with _owner_conn() as conn:
            eid = _uid("e_wo9_")
            _seed_experiment(conn, WS_A, eid, self.ids["prod_id"],
                            schema_version=None, state="draft")
            asset_version_id = _uid("av_")
            _seed_asset_version(
                conn, WS_A, self.ids["prod_id"], asset_version_id)
            conn.execute(
                "INSERT INTO experiment_entries "
                "(id,workspace_id,experiment_id,asset_version_id,"
                "marketing_vars_json,position,created_at) "
                "VALUES (%s,%s,%s,%s,'{}',0,NOW())",
                (entry_id, WS_A, eid, asset_version_id))
            conn.execute(
                "INSERT INTO experiment_ad_bindings "
                "(id,workspace_id,experiment_entry_id,ad_account_id,ad_id,"
                "source,created_at) "
                "VALUES (%s,%s,%s,%s,%s,'manual_read_only',NOW())",
                (binding_id, WS_A, entry_id, _uid("act_"), _uid("ad_")))
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "UPDATE experiment_ad_bindings "
                    "SET record_schema_version=2 WHERE id=%s",
                    (binding_id,))

    def test_WO10_ad_bindings_schema_2_to_null_rejected(self):
        entry_id = _uid("entry_wo10_")
        binding_id = _uid("bind_wo10_")
        with _owner_conn() as conn:
            eid = _uid("e_wo10_")
            _seed_experiment(conn, WS_A, eid, self.ids["prod_id"],
                            schema_version=2, state="draft")
            asset_version_id = _uid("av_")
            _seed_asset_version(
                conn, WS_A, self.ids["prod_id"], asset_version_id)
            conn.execute(
                "INSERT INTO experiment_entries "
                "(id,workspace_id,experiment_id,asset_version_id,"
                "marketing_vars_json,position,created_at) "
                "VALUES (%s,%s,%s,%s,'{}',0,NOW())",
                (entry_id, WS_A, eid, asset_version_id))
            conn.execute(
                "INSERT INTO experiment_ad_bindings "
                "(id,workspace_id,experiment_entry_id,ad_account_id,ad_id,"
                "source,record_schema_version,created_at) "
                "VALUES (%s,%s,%s,%s,%s,'manual_read_only',2,NOW())",
                (binding_id, WS_A, entry_id, _uid("act_"), _uid("ad_")))
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "UPDATE experiment_ad_bindings "
                    "SET record_schema_version=NULL WHERE id=%s",
                    (binding_id,))

    def test_WO11_qc_attempts_sequence_write_once(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT id FROM asset_qc_attempts LIMIT 1"
            ).fetchone()
            if row is None:
                self.skipTest("No asset_qc_attempts rows")
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "UPDATE asset_qc_attempts SET attempt_sequence=99 "
                    "WHERE id=%s", (row[0],))

    def test_WO12_qc_attempts_remediates_write_once(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT id FROM asset_qc_attempts "
                "WHERE remediates_attempt_id IS NOT NULL LIMIT 1"
            ).fetchone()
            if row is None:
                self.skipTest("No asset_qc_attempts with remediates")
            with self.assertRaises(psycopg.errors.RaiseException):
                conn.execute(
                    "UPDATE asset_qc_attempts "
                    "SET remediates_attempt_id=%s WHERE id=%s",
                    (_uid("wrong_"), row[0]))


# ===================================================================
# §5.6.5 — RLS, FORCE RLS, signed tenant context (RL-01..RL-15)
# ===================================================================

@_skip_no_pg
class TestRL(unittest.TestCase):
    """RLS, FORCE RLS, and signed tenant context."""

    @classmethod
    def setUpClass(cls):
        with _owner_conn() as conn:
            cls.ids = _seed_full_lineage(conn, WS_A, "rl")
            conn.commit()

    def test_RL01_rls_enabled_on_all_new_tables(self):
        with _owner_conn() as conn:
            for table in NEW_WORKSPACE_OWNED:
                with self.subTest(table=table):
                    row = conn.execute(
                        "SELECT relrowsecurity FROM pg_class "
                        "WHERE relname = %s", (table,)
                    ).fetchone()
                    self.assertIsNotNone(row, f"{table} not found")
                    self.assertTrue(row[0], f"{table} RLS not enabled")

    def test_RL02_force_rls_on_all_new_tables(self):
        with _owner_conn() as conn:
            for table in NEW_WORKSPACE_OWNED:
                with self.subTest(table=table):
                    row = conn.execute(
                        "SELECT relforcerowsecurity FROM pg_class "
                        "WHERE relname = %s", (table,)
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertTrue(row[0], f"{table} FORCE RLS not set")

    def test_RL03_using_policy_exists(self):
        with _owner_conn() as conn:
            for table in NEW_WORKSPACE_OWNED:
                with self.subTest(table=table):
                    rows = conn.execute(
                        "SELECT policyname FROM pg_policies "
                        "WHERE tablename = %s", (table,)
                    ).fetchall()
                    self.assertTrue(len(rows) > 0, f"{table}: no policies")

    def test_RL04_tenant_isolation_policy_exists(self):
        with _owner_conn() as conn:
            for table in NEW_WORKSPACE_OWNED:
                with self.subTest(table=table):
                    row = conn.execute(
                        "SELECT policyname FROM pg_policies "
                        "WHERE tablename = %s "
                        "AND policyname LIKE 'tenant_isolation%%'",
                        (table,),
                    ).fetchone()
                    self.assertIsNotNone(row, f"{table}: no tenant_isolation")

    def test_RL05_api_cross_workspace_select_empty(self):
        with _api_conn(workspace_id=WS_B) as conn:
            rows = conn.execute(
                "SELECT id FROM experiment_versions "
                "WHERE workspace_id = %s", (WS_A,)
            ).fetchall()
            self.assertEqual(len(rows), 0)

    def test_RL06_api_cross_workspace_insert_rejected(self):
        with _api_conn(workspace_id=WS_B) as conn:
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "INSERT INTO experiment_timeline_heads "
                    "(id,workspace_id,experiment_id,head_sequence) "
                    "VALUES (%s,%s,%s,0)",
                    (_uid("eth_"), WS_A, _uid("exp_")))
            self.assertIn(
                "row-level security", str(ctx.exception).lower())

    def test_RL07_valid_signed_context_returns_true(self):
        with _api_conn(workspace_id=WS_A) as conn:
            result = conn.execute(
                "SELECT marketai_context_is_current(%s)", (WS_A,)
            ).fetchone()[0]
            self.assertTrue(result)

    def test_RL08_missing_workspace_guc_returns_false(self):
        with _pg_conn("marketai_domain_api") as conn:
            epoch = _get_epoch(conn, WS_A)
            sig = _sign_context(WS_A, epoch)
            _set_local(conn, "marketai.authorization_epoch", epoch)
            _set_local(conn, "marketai.context_signature", sig)
            result = conn.execute(
                "SELECT marketai_context_is_current(%s)", (WS_A,)
            ).fetchone()[0]
            self.assertFalse(result)

    def test_RL09_missing_epoch_guc_returns_false(self):
        with _pg_conn("marketai_domain_api") as conn:
            sig = _sign_context(WS_A, 1)
            _set_local(conn, "marketai.workspace_id", WS_A)
            _set_local(conn, "marketai.context_signature", sig)
            result = conn.execute(
                "SELECT marketai_context_is_current(%s)", (WS_A,)
            ).fetchone()[0]
            self.assertFalse(result)

    def test_RL10_missing_signature_guc_returns_false(self):
        with _pg_conn("marketai_domain_api") as conn:
            epoch = _get_epoch(conn, WS_A)
            _set_local(conn, "marketai.workspace_id", WS_A)
            _set_local(conn, "marketai.authorization_epoch", epoch)
            result = conn.execute(
                "SELECT marketai_context_is_current(%s)", (WS_A,)
            ).fetchone()[0]
            self.assertFalse(result)

    def test_RL11_stale_epoch_returns_false(self):
        with _pg_conn("marketai_domain_api") as conn:
            epoch = _get_epoch(conn, WS_A)
            stale = epoch - 100
            sig = _sign_context(WS_A, stale)
            _set_local(conn, "marketai.workspace_id", WS_A)
            _set_local(conn, "marketai.authorization_epoch", stale)
            _set_local(conn, "marketai.context_signature", sig)
            result = conn.execute(
                "SELECT marketai_context_is_current(%s)", (WS_A,)
            ).fetchone()[0]
            self.assertFalse(result)

    def test_RL12_bad_signature_returns_false(self):
        with _pg_conn("marketai_domain_api") as conn:
            epoch = _get_epoch(conn, WS_A)
            _set_local(conn, "marketai.workspace_id", WS_A)
            _set_local(conn, "marketai.authorization_epoch", epoch)
            _set_local(conn, "marketai.context_signature", "deadbeef" * 8)
            result = conn.execute(
                "SELECT marketai_context_is_current(%s)", (WS_A,)
            ).fetchone()[0]
            self.assertFalse(result)

    def test_RL13_cross_workspace_signed_empty(self):
        with _api_conn(workspace_id=WS_B) as conn:
            rows = conn.execute(
                "SELECT id FROM experiment_versions "
                "WHERE workspace_id = %s", (WS_A,)
            ).fetchall()
            self.assertEqual(len(rows), 0)

    def test_RL14_force_rls_non_bypassrls_cross_workspace(self):
        with _api_conn(workspace_id=WS_B) as conn:
            rows = conn.execute(
                "SELECT id FROM experiment_versions"
            ).fetchall()
            ws_a_ids = [
                r[0] for r in rows
                if r[0].startswith("ver_rl")
            ]
            self.assertEqual(len(ws_a_ids), 0)

    def test_RL15_force_rls_insert_cross_workspace_rejected(self):
        with _api_conn(workspace_id=WS_B) as conn:
            with self.assertRaises(Exception):
                conn.execute(
                    "INSERT INTO experiment_timeline_heads "
                    "(id,workspace_id,experiment_id,head_sequence) "
                    "VALUES (%s,%s,%s,0)",
                    (_uid("eth_"), WS_A, _uid("exp_")))


# ===================================================================
# §5.6.6 — Role and grant matrix (RG-01..RG-22)
# ===================================================================

@_skip_no_pg
class TestRG(unittest.TestCase):
    """Role and grant matrix."""

    def _has_priv(self, conn, role, table, priv):
        return conn.execute(
            "SELECT has_table_privilege(%s, %s, %s)",
            (role, table, priv),
        ).fetchone()[0]

    def test_RG01_api_select_immutable(self):
        with _owner_conn() as conn:
            for t in IMMUTABLE_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertTrue(
                        self._has_priv(conn, "marketai_domain_api", t, "SELECT"))

    def test_RG02_api_insert_immutable(self):
        with _owner_conn() as conn:
            for t in IMMUTABLE_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertTrue(
                        self._has_priv(conn, "marketai_domain_api", t, "INSERT"))

    def test_RG03_api_update_immutable_denied(self):
        with _owner_conn() as conn:
            for t in IMMUTABLE_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertFalse(
                        self._has_priv(conn, "marketai_domain_api", t, "UPDATE"))

    def test_RG04_api_delete_immutable_denied(self):
        with _owner_conn() as conn:
            for t in IMMUTABLE_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertFalse(
                        self._has_priv(conn, "marketai_domain_api", t, "DELETE"))

    def test_RG06_api_verdict_projections(self):
        with _owner_conn() as conn:
            self.assertTrue(self._has_priv(
                conn, "marketai_domain_api",
                "contextual_verdict_projections", "SELECT"))
            self.assertTrue(self._has_priv(
                conn, "marketai_domain_api",
                "contextual_verdict_projections", "INSERT"))

    def test_RG07_api_verdict_projections_delete_denied(self):
        with _owner_conn() as conn:
            self.assertFalse(self._has_priv(
                conn, "marketai_domain_api",
                "contextual_verdict_projections", "DELETE"))

    def test_RG08_worker_same_as_api(self):
        with _owner_conn() as conn:
            for t in IMMUTABLE_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertTrue(self._has_priv(
                        conn, "marketai_domain_worker", t, "SELECT"))
                    self.assertTrue(self._has_priv(
                        conn, "marketai_domain_worker", t, "INSERT"))
                    self.assertFalse(self._has_priv(
                        conn, "marketai_domain_worker", t, "UPDATE"))
                    self.assertFalse(self._has_priv(
                        conn, "marketai_domain_worker", t, "DELETE"))

    def test_RG09_projection_writer_select_immutable(self):
        with _owner_conn() as conn:
            for t in IMMUTABLE_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertTrue(self._has_priv(
                        conn, "marketai_projection_writer", t, "SELECT"))

    def test_RG10_projection_writer_no_write_immutable(self):
        with _owner_conn() as conn:
            for t in IMMUTABLE_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    for priv in ("INSERT", "UPDATE", "DELETE"):
                        self.assertFalse(self._has_priv(
                            conn, "marketai_projection_writer", t, priv))

    def test_RG11_projection_writer_eligibility_select_update(self):
        with _owner_conn() as conn:
            self.assertTrue(self._has_priv(
                conn, "marketai_projection_writer",
                "experiment_eligibility_projections", "SELECT"))
            self.assertTrue(self._has_priv(
                conn, "marketai_projection_writer",
                "experiment_eligibility_projections", "UPDATE"))

    def test_RG12_projection_writer_eligibility_no_insert_delete(self):
        with _owner_conn() as conn:
            self.assertFalse(self._has_priv(
                conn, "marketai_projection_writer",
                "experiment_eligibility_projections", "INSERT"))
            self.assertFalse(self._has_priv(
                conn, "marketai_projection_writer",
                "experiment_eligibility_projections", "DELETE"))

    def test_RG13_projection_writer_verdict_full_dml(self):
        with _owner_conn() as conn:
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                with self.subTest(priv=priv):
                    self.assertTrue(self._has_priv(
                        conn, "marketai_projection_writer",
                        "contextual_verdict_projections", priv))

    def test_RG14_readonly_select_all(self):
        with _owner_conn() as conn:
            for t in NEW_WORKSPACE_OWNED:
                with self.subTest(table=t):
                    self.assertTrue(self._has_priv(
                        conn, "marketai_domain_readonly", t, "SELECT"))

    def test_RG15b_erasure_principal_no_bypassrls(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT rolbypassrls FROM pg_roles "
                "WHERE rolname = 'marketai_erasure_principal'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertFalse(row[0])

    def test_RG17_readonly_no_write(self):
        with _owner_conn() as conn:
            for t in NEW_WORKSPACE_OWNED:
                with self.subTest(table=t):
                    for priv in ("INSERT", "UPDATE", "DELETE"):
                        self.assertFalse(self._has_priv(
                            conn, "marketai_domain_readonly", t, priv))

    def test_RG18_api_cannot_set_role_projection_writer(self):
        with _api_conn(workspace_id=WS_A) as conn:
            with self.assertRaises(Exception) as ctx:
                conn.execute("SET ROLE marketai_projection_writer")
            err = str(ctx.exception).lower()
            self.assertTrue(
                "permission denied" in err or "not a member" in err)

    def test_RG20_owner_bypassrls(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT rolbypassrls FROM pg_roles "
                "WHERE rolname = 'marketai_domain_owner'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(row[0])

    def test_RG21_execute_context_function(self):
        func_sig = "marketai_context_is_current(text)"
        with _owner_conn() as conn:
            for role in ("marketai_domain_api", "marketai_domain_worker",
                         "marketai_domain_readonly"):
                with self.subTest(role=role):
                    result = conn.execute(
                        "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                        (role, func_sig),
                    ).fetchone()[0]
                    self.assertTrue(result)

    def test_RG22_execute_context_function_public_denied(self):
        with _owner_conn() as conn:
            self.assertFalse(_public_can_execute(
                conn, "marketai_context_is_current(text)"))


# ===================================================================
# §5.6.7 — SECURITY DEFINER functions (SF)
# ===================================================================

@_skip_no_pg
class TestSF(unittest.TestCase):
    """SECURITY DEFINER functions."""

    def _func_props(self, conn, func_name):
        return conn.execute(
            "SELECT r.rolname, p.prosecdef, p.proconfig "
            "FROM pg_proc p JOIN pg_roles r ON p.proowner = r.oid "
            "WHERE p.proname = %s", (func_name,)
        ).fetchone()

    def test_SF01_reject_function_exists(self):
        with _owner_conn() as conn:
            row = self._func_props(conn, "marketai_reject_update_or_delete")
            self.assertIsNotNone(row)

    def test_SF_reject_function_search_path(self):
        with _owner_conn() as conn:
            row = self._func_props(conn, "marketai_reject_update_or_delete")
            self.assertIsNotNone(row)
            if row[2]:
                self.assertIn("search_path", str(row[2]))

    def test_SF_eligibility_delete_function_exists(self):
        with _owner_conn() as conn:
            row = self._func_props(conn, "marketai_reject_eligibility_delete")
            self.assertIsNotNone(row)
            if row[2]:
                self.assertIn("search_path", str(row[2]))

    def test_SF_timeline_head_identity_function_exists(self):
        with _owner_conn() as conn:
            row = self._func_props(
                conn, "marketai_reject_timeline_head_identity_change")
            self.assertIsNotNone(row)

    def test_SF10_api_direct_delete_verdict_denied(self):
        with _owner_conn() as conn:
            has = conn.execute(
                "SELECT has_table_privilege('marketai_domain_api', "
                "'contextual_verdict_projections', 'DELETE')"
            ).fetchone()[0]
            self.assertFalse(has)

    def test_SF12_context_function_security_definer(self):
        with _owner_conn() as conn:
            row = self._func_props(conn, "marketai_context_is_current")
            self.assertIsNotNone(row)
            self.assertTrue(row[1], "Not SECURITY DEFINER")

    def test_SF12_context_function_owner(self):
        with _owner_conn() as conn:
            row = self._func_props(conn, "marketai_context_is_current")
            self.assertIsNotNone(row)
            self.assertNotEqual(row[0], "marketai_domain_api")
            self.assertNotEqual(row[0], "marketai_domain_worker")

    def test_SF13_context_function_public_revoked(self):
        with _owner_conn() as conn:
            self.assertFalse(_public_can_execute(
                conn, "marketai_context_is_current(text)"))

    def test_SF13_context_function_runtime_execute(self):
        func_sig = "marketai_context_is_current(text)"
        with _owner_conn() as conn:
            for role in ("marketai_domain_api", "marketai_domain_worker",
                         "marketai_domain_readonly"):
                with self.subTest(role=role):
                    self.assertTrue(conn.execute(
                        "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                        (role, func_sig),
                    ).fetchone()[0])

    def test_SF13a_context_function_search_path(self):
        with _owner_conn() as conn:
            row = self._func_props(conn, "marketai_context_is_current")
            self.assertIsNotNone(row)
            if row[2]:
                self.assertIn("search_path", str(row[2]))

    def test_SF13b_context_function_fail_closed(self):
        with _pg_conn("marketai_domain_api") as conn:
            result = conn.execute(
                "SELECT marketai_context_is_current(%s)", (WS_A,)
            ).fetchone()[0]
            self.assertFalse(result)

    def test_SF15_projection_writer_grants(self):
        with _owner_conn() as conn:
            for t in IMMUTABLE_EVIDENCE_TABLES:
                self.assertTrue(conn.execute(
                    "SELECT has_table_privilege("
                    "'marketai_projection_writer', %s, 'SELECT')", (t,)
                ).fetchone()[0])
            self.assertTrue(conn.execute(
                "SELECT has_table_privilege("
                "'marketai_projection_writer', "
                "'experiment_eligibility_projections', 'SELECT')"
            ).fetchone()[0])
            self.assertTrue(conn.execute(
                "SELECT has_table_privilege("
                "'marketai_projection_writer', "
                "'experiment_eligibility_projections', 'UPDATE')"
            ).fetchone()[0])
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                self.assertTrue(conn.execute(
                    "SELECT has_table_privilege("
                    "'marketai_projection_writer', "
                    "'contextual_verdict_projections', %s)", (priv,)
                ).fetchone()[0])


# ===================================================================
# §5.6.8 — Controlled erasure (ER-01..ER-07a)
# ===================================================================

@_skip_no_pg
class TestER(unittest.TestCase):
    """Controlled erasure tests."""

    @classmethod
    def setUpClass(cls):
        with _owner_conn() as conn:
            _seed_product(conn, WS_A, "prod_er")
            _seed_experiment(conn, WS_A, "exp_er", "prod_er",
                            schema_version=None, state="draft")
            conn.execute(
                "INSERT INTO experiment_state_events "
                "(id,workspace_id,experiment_id,from_state,to_state,revision,"
                "actor_id,reason,created_at) "
                "VALUES (%s,%s,'exp_er',NULL,'draft',1,'test','seed',NOW()) "
                "ON CONFLICT DO NOTHING",
                (_uid("ese_er_"), WS_A))
            conn.commit()

    def test_ER03_non_erasure_delete_rejected(self):
        with _owner_conn() as conn:
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "DELETE FROM experiment_state_events "
                    "WHERE experiment_id = 'exp_er'")
            self.assertIn("DELETE denied", str(ctx.exception))

    def test_ER05_api_erasure_guc_delete_rejected(self):
        with _api_conn(workspace_id=WS_A) as conn:
            _set_local(conn, "marketai.erasure_workspace_id", WS_A)
            with self.assertRaises(Exception):
                conn.execute(
                    "DELETE FROM experiment_state_events "
                    "WHERE workspace_id = %s", (WS_A,))

    def test_ER07_erasure_procedure_owner_bypassrls(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT rolbypassrls FROM pg_roles "
                "WHERE rolname = 'marketai_domain_owner'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(row[0])

    def test_ER07a_erasure_caller_no_bypassrls(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT rolbypassrls FROM pg_roles "
                "WHERE rolname = 'marketai_erasure_principal'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertFalse(row[0])


# ===================================================================
# §5.6.9 — Concurrency and deadlock (DC-01..DC-06)
# ===================================================================

@_skip_no_pg
class TestDC(unittest.TestCase):
    """Concurrency and deadlock tests using threading.Barrier."""

    @classmethod
    def setUpClass(cls):
        with _owner_conn() as conn:
            _seed_product(conn, WS_A, "prod_dc")
            for s in ("a", "b", "c", "d"):
                _seed_experiment(conn, WS_A, f"exp_dc_{s}", "prod_dc",
                                schema_version=None, state="draft")
            conn.commit()

    def test_DC01_concurrent_freeze_different_experiments(self):
        barrier = threading.Barrier(2, timeout=10)
        results = [None, None]

        def worker(idx, eid):
            try:
                with _api_conn(workspace_id=WS_A) as conn:
                    conn.execute("SET LOCAL lock_timeout = '5s'")
                    barrier.wait()
                    row = conn.execute(
                        "SELECT id, state FROM experiments "
                        "WHERE id = %s FOR UPDATE", (eid,)
                    ).fetchone()
                    results[idx] = "success" if row else "not_found"
            except Exception as e:
                results[idx] = f"error:{e}"

        t1 = threading.Thread(target=worker, args=(0, "exp_dc_a"))
        t2 = threading.Thread(target=worker, args=(1, "exp_dc_b"))
        t1.start(); t2.start()
        t1.join(15); t2.join(15)
        self.assertEqual(results[0], "success")
        self.assertEqual(results[1], "success")

    def test_DC02_concurrent_same_experiment_serialized(self):
        barrier = threading.Barrier(2, timeout=10)
        results = [None, None]

        def worker(idx, op):
            try:
                with _api_conn(workspace_id=WS_A) as conn:
                    conn.execute("SET LOCAL lock_timeout = '5s'")
                    barrier.wait()
                    conn.execute(
                        "SELECT id FROM experiments "
                        "WHERE id = 'exp_dc_c' FOR UPDATE")
                    results[idx] = f"{op}:success"
            except Exception as e:
                results[idx] = f"{op}:error:{e}"

        t1 = threading.Thread(target=worker, args=(0, "freeze"))
        t2 = threading.Thread(target=worker, args=(1, "start"))
        t1.start(); t2.start()
        t1.join(15); t2.join(15)
        successes = [r for r in results if r and "success" in r]
        self.assertGreaterEqual(len(successes), 1)

    def test_DC04b_duplicate_insert_unique_violation(self):
        dup_id = _uid("dup_")
        with _api_conn(workspace_id=WS_A) as conn:
            conn.execute(
                "INSERT INTO experiment_state_events "
                "(id,workspace_id,experiment_id,from_state,to_state,revision,"
                "actor_id,reason,created_at) "
                "VALUES (%s,%s,'exp_dc_a',NULL,'draft',1,'test','dup',NOW())",
                (dup_id, WS_A))
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "INSERT INTO experiment_state_events "
                    "(id,workspace_id,experiment_id,from_state,to_state,"
                    "revision,actor_id,reason,created_at) "
                    "VALUES (%s,%s,'exp_dc_a',NULL,'draft',1,'test',"
                    "'dup',NOW())",
                    (dup_id, WS_A))
            err = str(ctx.exception).lower()
            self.assertTrue(
                "unique" in err or "duplicate" in err)

    def test_DC06_lock_timeout_sqlstate(self):
        barrier = threading.Barrier(2, timeout=10)
        holder_ready = threading.Event()
        results = [None, None]

        def holder():
            try:
                with _api_conn(workspace_id=WS_A) as conn:
                    conn.execute(
                        "SELECT id FROM experiments "
                        "WHERE id = 'exp_dc_d' FOR UPDATE")
                    holder_ready.set()
                    barrier.wait()
            except Exception as e:
                results[0] = f"holder_error:{e}"
                holder_ready.set()

        def waiter():
            holder_ready.wait(timeout=10)
            try:
                with _api_conn(workspace_id=WS_A) as conn:
                    conn.execute("SET LOCAL lock_timeout = '1s'")
                    conn.execute(
                        "SELECT id FROM experiments "
                        "WHERE id = 'exp_dc_d' FOR UPDATE")
                    results[1] = "unexpected_success"
            except Exception as e:
                err_str = str(e)
                if "55P03" in err_str or "lock timeout" in err_str.lower():
                    results[1] = "lock_timeout"
                else:
                    results[1] = f"unexpected_error:{e}"
            finally:
                barrier.wait()

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start(); t2.start()
        t1.join(15); t2.join(15)
        self.assertEqual(results[1], "lock_timeout")


# ===================================================================
# §5.6.10 — PostgreSQL trigger catalog (TC-01..TC-05)
# ===================================================================

WRITE_ONCE_TRIGGERS = {
    "products": "trg_products_profile_set_once",
    "experiments": "trg_experiments_schema_version_set_once",
    "experiment_ad_bindings": "trg_ad_bindings_schema_set_once",
    "asset_versions": "trg_asset_versions_truth_write_once",
    "asset_qc_attempts": "trg_qc_attempts_seq_write_once",
}

WRITE_ONCE_TRIGGER_STARTED_AT = {
    "experiments": "trg_experiments_started_at_set_once",
}


@_skip_no_pg
class TestTC(unittest.TestCase):
    """PostgreSQL trigger catalog."""

    def test_TC01_reject_function_exists(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_proc "
                "WHERE proname = 'marketai_reject_update_or_delete'"
            ).fetchone()
            self.assertIsNotNone(row)

    def test_TC02_append_only_triggers_exist(self):
        with _owner_conn() as conn:
            for table in APPEND_ONLY_TABLES:
                trigger_name = f"trg_{table}_append_only"
                with self.subTest(table=table, trigger=trigger_name):
                    row = conn.execute(
                        "SELECT 1 FROM pg_trigger "
                        "WHERE tgname = %s "
                        "AND tgrelid = to_regclass(%s)::oid",
                        (trigger_name, table),
                    ).fetchone()
                    self.assertIsNotNone(row, f"Missing {trigger_name}")

    def test_TC03_write_once_triggers_exist(self):
        all_triggers = {**WRITE_ONCE_TRIGGERS, **WRITE_ONCE_TRIGGER_STARTED_AT}
        with _owner_conn() as conn:
            for table, trigger_name in all_triggers.items():
                with self.subTest(table=table, trigger=trigger_name):
                    row = conn.execute(
                        "SELECT 1 FROM pg_trigger "
                        "WHERE tgname = %s "
                        "AND tgrelid = to_regclass(%s)::oid",
                        (trigger_name, table),
                    ).fetchone()
                    self.assertIsNotNone(row, f"Missing {trigger_name}")

    def test_TC04_no_create_trigger_if_not_exists(self):
        import pathlib
        src = pathlib.Path(
            "migrations/versions/"
            "t1a2b3c4d5e6_immutable_contextual_decisions.py"
        ).read_text()
        self.assertNotIn("CREATE TRIGGER IF NOT EXISTS", src)

    def test_TC05_old_per_table_function_removed(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_proc WHERE proname = "
                "'reject_update_delete_asset_qc_check_results'"
            ).fetchone()
            self.assertIsNone(row)

    def test_TC_eligibility_no_delete_trigger_exists(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_eep_no_delete' "
                "AND tgrelid = "
                "to_regclass('experiment_eligibility_projections')::oid"
            ).fetchone()
            self.assertIsNotNone(row)

    def test_TC_timeline_head_identity_trigger_exists(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_eth_identity_immutable' "
                "AND tgrelid = "
                "to_regclass('experiment_timeline_heads')::oid"
            ).fetchone()
            self.assertIsNotNone(row)


# ===================================================================
# §5.11.7 — Security-negative tests (SN-01..SN-10)
# ===================================================================

@_skip_no_pg
class TestSN(unittest.TestCase):
    """Security-negative tests."""

    @classmethod
    def setUpClass(cls):
        with _owner_conn() as conn:
            _seed_product(conn, WS_A, "prod_sn")
            _seed_experiment(conn, WS_A, "exp_sn", "prod_sn",
                            schema_version=None, state="draft")
            conn.commit()

    def test_SN01_readonly_insert_denied(self):
        with _readonly_conn(workspace_id=WS_A) as conn:
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "INSERT INTO experiment_state_events "
                    "(id,workspace_id,experiment_id,from_state,to_state,"
                    "revision,actor_id,reason,created_at) "
                    "VALUES ('sn01',%s,'exp_sn',NULL,'draft',1,'test',"
                    "'sn',NOW())", (WS_A,))
            self.assertIn("permission denied", str(ctx.exception).lower())

    def test_SN02_api_delete_append_only_denied(self):
        with _api_conn(workspace_id=WS_A) as conn:
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "DELETE FROM experiment_versions "
                    "WHERE workspace_id = %s", (WS_A,))
            err = str(ctx.exception).lower()
            self.assertTrue("denied" in err)

    def test_SN03_api_set_role_projection_writer_denied(self):
        with _api_conn(workspace_id=WS_A) as conn:
            with self.assertRaises(Exception) as ctx:
                conn.execute("SET ROLE marketai_projection_writer")
            self.assertIn("permission denied", str(ctx.exception).lower())

    def test_SN06_cross_workspace_read_empty(self):
        with _api_conn(workspace_id=WS_B) as conn:
            rows = conn.execute(
                "SELECT id FROM experiments WHERE id = 'exp_sn'"
            ).fetchall()
            self.assertEqual(len(rows), 0)

    def test_SN07_cross_workspace_write_rejected(self):
        with _api_conn(workspace_id=WS_B) as conn:
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "INSERT INTO experiments "
                    "(id,workspace_id,product_id,name,state,revision,"
                    "config_json,metric_contract_hash,created_at) "
                    "VALUES ('sn07',%s,'prod_sn','cross','draft',1,'{}',"
                    "%s,NOW())",
                    (WS_A, H64))
            err = str(ctx.exception).lower()
            self.assertTrue(
                "policy" in err or "security" in err or "violat" in err)

    def test_SN08_schema_version_check_violation(self):
        with _owner_conn() as conn:
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "INSERT INTO experiments "
                    "(id,workspace_id,product_id,name,state,revision,"
                    "config_json,metric_contract_hash,"
                    "record_schema_version,created_at) "
                    "VALUES ('sn08',%s,'prod_sn','bad','draft',1,'{}',"
                    "%s,3,NOW())",
                    (WS_A, H64))
            err = str(ctx.exception).lower()
            self.assertTrue("check" in err or "violat" in err)

    def test_SN09_stale_epoch_returns_false(self):
        with _pg_conn("marketai_domain_api") as conn:
            _set_local(conn, "marketai.workspace_id", WS_A)
            conn.execute(
                "SET LOCAL marketai.authorization_epoch = '99999'")
            conn.execute(
                "SET LOCAL marketai.context_signature = 'bad'")
            result = conn.execute(
                "SELECT marketai_context_is_current(%s)", (WS_A,)
            ).fetchone()[0]
            self.assertFalse(result)

    def test_SN10_forged_signature_returns_false(self):
        with _pg_conn("marketai_domain_api") as conn:
            _set_local(conn, "marketai.workspace_id", WS_A)
            conn.execute(
                "SET LOCAL marketai.authorization_epoch = '0'")
            conn.execute(
                "SET LOCAL marketai.context_signature = 'forged_sig'")
            result = conn.execute(
                "SELECT marketai_context_is_current(%s)", (WS_A,)
            ).fetchone()[0]
            self.assertFalse(result)


# ===================================================================
# §5.5.3 PG-only — Truth-dependency (TD-07..TD-10)
# ===================================================================

@_skip_no_pg
class TestTD(unittest.TestCase):
    """Truth-dependency trigger tests (PG-only)."""

    @classmethod
    def setUpClass(cls):
        with _owner_conn() as conn:
            _seed_product(conn, WS_A, "prod_td")
            conn.execute(
                "INSERT INTO plans (id,workspace_id,product_id,mode,cap,"
                "total_full,snapshot_hash,created_at) "
                "VALUES ('plan_td',%s,'prod_td','full',0,1,%s,NOW()) "
                "ON CONFLICT DO NOTHING", (WS_A, H64))
            conn.execute(
                "INSERT INTO generation_runs "
                "(id,workspace_id,product_id,plan_id,provider,model,"
                "aspect_ratio,image_size,variants,use_ref,status,"
                "total,done,failed,est_cost,currency,revision,created_at,updated_at) "
                "VALUES ('run_td',%s,'prod_td','plan_td','mock','mock',"
                "'1:1','1024',1,false,'done',1,1,0,0,'USD',1,NOW(),NOW()) "
                "ON CONFLICT DO NOTHING", (WS_A,))
            conn.commit()

    def _make_asset_and_version(self, conn, tag, truth_fields=None):
        asset_id = f"asset_td_{tag}"
        av_id = f"av_td_{tag}"
        conn.execute(
            "INSERT INTO assets "
            "(id,workspace_id,product_id,run_id,combo_id,combo_code,"
            "tags_json,filename,provider,model,prompt,fingerprint,"
            "status,error_code,error_detail,created_at,updated_at) "
            "VALUES (%s,%s,'prod_td','run_td','C01','A1','{}','td.png',"
            "'mock','mock','test',%s,'ok','','',NOW(),NOW())",
            (asset_id, WS_A, f"fp_{tag}"))
        if truth_fields:
            conn.execute(
                "INSERT INTO asset_versions "
                "(id,workspace_id,asset_id,kind,aspect_ratio,language,"
                "object_key,content_sha256,byte_size,mime_type,"
                "truth_dependency_expected,truth_dependency_policy_version,"
                "truth_dependency_evidence_json,"
                "truth_dependency_evidence_hash,"
                "created_at) "
                "VALUES (%s,%s,%s,'generated','1:1','en','td.png',%s,1,"
                "'image/png',%s,%s,%s,%s,NOW())",
                (av_id, WS_A, asset_id, H64,
                 truth_fields["expected"], truth_fields["policy"],
                 truth_fields.get("json", '{"ok":true}'),
                 truth_fields.get("hash", "e" * 64)))
        else:
            conn.execute(
                "INSERT INTO asset_versions "
                "(id,workspace_id,asset_id,kind,aspect_ratio,language,"
                "object_key,content_sha256,byte_size,mime_type,"
                "created_at) "
                "VALUES (%s,%s,%s,'generated','1:1','en','td.png',%s,1,"
                "'image/png',NOW())",
                (av_id, WS_A, asset_id, H64))
        return av_id

    def test_TD07_truth_write_once_change_rejected(self):
        with _owner_conn() as conn:
            av_id = self._make_asset_and_version(conn, _uid("td07_"), {
                "expected": True, "policy": "v2-live"})
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "UPDATE asset_versions "
                    "SET truth_dependency_expected = false WHERE id = %s",
                    (av_id,))
            self.assertIn("write-once", str(ctx.exception))

    def test_TD08_v1_bootstrap_insert_rejected(self):
        with _owner_conn() as conn:
            asset_id = f"asset_td08_{_uid()}"
            av_id = f"av_td08_{_uid()}"
            conn.execute(
                "INSERT INTO assets "
                "(id,workspace_id,product_id,run_id,combo_id,combo_code,"
                "tags_json,filename,provider,model,prompt,fingerprint,"
                "status,error_code,error_detail,created_at,updated_at) "
                "VALUES (%s,%s,'prod_td','run_td','C01','A1','{}','td.png',"
                "'mock','mock','test',%s,'ok','','',NOW(),NOW())",
                (asset_id, WS_A, f"fp_{av_id}"))
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "INSERT INTO asset_versions "
                    "(id,workspace_id,asset_id,kind,aspect_ratio,language,"
                    "object_key,content_sha256,byte_size,mime_type,"
                    "truth_dependency_expected,"
                    "truth_dependency_policy_version,"
                    "truth_dependency_evidence_json,"
                    "truth_dependency_evidence_hash,"
                    "created_at) "
                    "VALUES (%s,%s,%s,'generated','1:1','en','td.png',"
                    "%s,1,'image/png',true,'v1-bootstrap',"
                    "'{\"ok\":true}',%s,NOW())",
                    (av_id, WS_A, asset_id, H64, "0" * 64))
            self.assertIn("v1-bootstrap", str(ctx.exception))

    def test_TD09_v1_bootstrap_update_init_rejected(self):
        with _owner_conn() as conn:
            av_id = self._make_asset_and_version(conn, _uid("td09_"))
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "UPDATE asset_versions SET "
                    "truth_dependency_expected = true, "
                    "truth_dependency_policy_version = 'v1-bootstrap', "
                    "truth_dependency_evidence_json = '{\"ok\":true}', "
                    "truth_dependency_evidence_hash = %s "
                    "WHERE id = %s",
                    ("2" * 64, av_id))
            self.assertIn("v1-bootstrap", str(ctx.exception))

    def test_TD10_atomic_four_field_init_allowed(self):
        with _owner_conn() as conn:
            av_id = self._make_asset_and_version(conn, _uid("td10_"))
            conn.execute(
                "UPDATE asset_versions SET "
                "truth_dependency_expected = true, "
                "truth_dependency_policy_version = 'v2-live', "
                "truth_dependency_evidence_json = '{\"ok\":true}', "
                "truth_dependency_evidence_hash = %s "
                "WHERE id = %s",
                ("4" * 64, av_id))
            row = conn.execute(
                "SELECT truth_dependency_expected, "
                "truth_dependency_policy_version "
                "FROM asset_versions WHERE id = %s",
                (av_id,)).fetchone()
            self.assertTrue(row[0])
            self.assertEqual(row[1], "v2-live")
            conn.rollback()

    def test_TD10_partial_init_rejected(self):
        with _owner_conn() as conn:
            av_id = self._make_asset_and_version(conn, _uid("td10p_"))
            with self.assertRaises(Exception) as ctx:
                conn.execute(
                    "UPDATE asset_versions SET "
                    "truth_dependency_expected = true, "
                    "truth_dependency_policy_version = 'v2-live' "
                    "WHERE id = %s", (av_id,))
            self.assertIn("atomically", str(ctx.exception))


# ===================================================================
# §5.14.9 — Addendum table integration (ATI)
# ===================================================================

@_skip_no_pg
class TestATI(unittest.TestCase):
    """Addendum table integration tests."""

    def _priv(self, conn, role, table, priv):
        return conn.execute(
            "SELECT has_table_privilege(%s, %s, %s)",
            (role, table, priv),
        ).fetchone()[0]

    def test_ATI01_rls_enabled(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_ALL_TABLES:
                with self.subTest(table=t):
                    row = conn.execute(
                        "SELECT relrowsecurity FROM pg_class "
                        "WHERE relname = %s", (t,)
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertTrue(row[0])

    def test_ATI02_force_rls(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_ALL_TABLES:
                with self.subTest(table=t):
                    row = conn.execute(
                        "SELECT relforcerowsecurity FROM pg_class "
                        "WHERE relname = %s", (t,)
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertTrue(row[0])

    def test_ATI08_api_select_insert_on_addendum(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    for priv in ("SELECT", "INSERT"):
                        self.assertTrue(
                            self._priv(conn, "marketai_domain_api", t, priv))

    def test_ATI09_api_update_addendum_denied(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertFalse(
                        self._priv(conn, "marketai_domain_api", t, "UPDATE"))

    def test_ATI10_api_delete_addendum_denied(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertFalse(
                        self._priv(conn, "marketai_domain_api", t, "DELETE"))

    def test_ATI11_worker_select_insert_addendum(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    for priv in ("SELECT", "INSERT"):
                        self.assertTrue(self._priv(
                            conn, "marketai_domain_worker", t, priv))

    def test_ATI14_readonly_select_addendum(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_ALL_TABLES:
                with self.subTest(table=t):
                    self.assertTrue(self._priv(
                        conn, "marketai_domain_readonly", t, "SELECT"))

    def test_ATI15_readonly_no_write_addendum(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_ALL_TABLES:
                with self.subTest(table=t):
                    for priv in ("INSERT", "UPDATE", "DELETE"):
                        self.assertFalse(self._priv(
                            conn, "marketai_domain_readonly", t, priv))

    def test_ATI23_projection_writer_no_write_evidence(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    for priv in ("INSERT", "UPDATE", "DELETE"):
                        self.assertFalse(self._priv(
                            conn, "marketai_projection_writer", t, priv))

    def test_ATI29_api_update_timeline_heads(self):
        with _owner_conn() as conn:
            self.assertTrue(self._priv(
                conn, "marketai_domain_api",
                "experiment_timeline_heads", "UPDATE"))

    def test_ATI30_worker_update_addendum_denied(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertFalse(self._priv(
                        conn, "marketai_domain_worker", t, "UPDATE"))

    def test_ATI31_worker_delete_addendum_denied(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_EVIDENCE_TABLES:
                with self.subTest(table=t):
                    self.assertFalse(self._priv(
                        conn, "marketai_domain_worker", t, "DELETE"))

    def test_ATI33_erasure_caller_no_bypassrls(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT rolbypassrls FROM pg_roles "
                "WHERE rolname = 'marketai_erasure_principal'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertFalse(row[0])

    def test_ATI34_erasure_procedure_owner_bypassrls(self):
        with _owner_conn() as conn:
            row = conn.execute(
                "SELECT rolbypassrls FROM pg_roles "
                "WHERE rolname = 'marketai_domain_owner'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertTrue(row[0])

    def test_ATI39_no_runtime_member_of_erasure_principal(self):
        runtime_roles = (
            "marketai_domain_api", "marketai_domain_worker",
            "marketai_domain_readonly", "marketai_projection_writer",
        )
        with _owner_conn() as conn:
            for role in runtime_roles:
                with self.subTest(role=role):
                    is_member = conn.execute(
                        "SELECT pg_has_role(%s, "
                        "'marketai_erasure_principal', 'MEMBER')",
                        (role,),
                    ).fetchone()[0]
                    self.assertFalse(is_member)

    def test_ATI40_projection_writer_select_addendum(self):
        with _owner_conn() as conn:
            for t in ADDENDUM_ALL_TABLES:
                with self.subTest(table=t):
                    self.assertTrue(self._priv(
                        conn, "marketai_projection_writer", t, "SELECT"))
