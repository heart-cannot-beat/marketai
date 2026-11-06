"""Lock order verification tests (LO-01..LO-05).

Verifies that service functions touch model classes in a consistent order and
that timeline operations (class 7) are performed after domain writes.

In SQLite there are no real row locks, so we instrument session.add() to
record the order of model class insertion and verify class membership and
relative ordering invariants. The full lock-class enforcement under real
row locks is tested at Integration-PG (§5.6.9).
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.adapters.persistence.database import engine, tenant_session
from app.adapters.persistence.models import (
    AdDailyMetricRecord,
    AssetRecord,
    AssetVersionRecord,
    ContextualVerdictProjectionRecord,
    DecisionConfirmationRecord,
    DecisionProposalRecord,
    DecisionProposalStateEventRecord,
    ExperimentAdBindingRecord,
    ExperimentDecisionRecord,
    ExperimentEligibilityProjectionRecord,
    ExperimentEntryRecord,
    ExperimentNotificationOutboxRecord,
    ExperimentRecord,
    ExperimentStateEventRecord,
    ExperimentTimelineEventRecord,
    ExperimentTimelineHeadRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    JudgementRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MetaAccountRecord,
    MetaSyncRunRecord,
    PlanRecord,
    ProductRecord,
    WorkspaceRecord,
)
from app.experiments.service import (
    add_entry_v2,
    bind_candidate_v2,
    confirm_proposal_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    run_judgement_v2,
    start_experiment_v2,
)
from app.platform.tenancy import TenantContext
from tests.experiment_governance_fixtures import (
    govern_asset_versions,
    govern_product_offer_versions,
)

CTX = TenantContext(
    workspace_id="ws_lo",
    role="owner",
    initiator_id="user_lo",
    request_id="req_lo",
    trace_id="trace_lo",
)

PROD_ID = "prod_lo"

LOCK_CLASS_MAP = {
    ExperimentRecord: 1,
    ExperimentStateEventRecord: 1,
    ExperimentVersionRecord: 2,
    ExperimentVersionCandidateRecord: 3,
    ExperimentEntryRecord: 4,
    ExperimentAdBindingRecord: 4,
    JudgementRecord: 5,
    DecisionProposalRecord: 5,
    DecisionProposalStateEventRecord: 5,
    ExperimentDecisionRecord: 6,
    DecisionConfirmationRecord: 6,
    ContextualVerdictProjectionRecord: 6,
    ExperimentTimelineHeadRecord: 7,
    ExperimentTimelineEventRecord: 7,
    ExperimentEligibilityProjectionRecord: 7,
    ExperimentNotificationOutboxRecord: 7,
}


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_lo",
        "offer_version_id": "ov_lo",
        "budget": {"daily_amount": "100", "total_amount": "3000"},
        "schedule": {
            "start_at": "2026-09-01T00:00:00+00:00",
            "end_at": "2026-12-01T00:00:00+00:00",
            "timezone": "UTC",
        },
        "attribution": {"click_window_days": 7, "view_window_days": 1},
        "statistical_plan": {
            "primary_metric": "ctr",
            "ctr_threshold": "0.01",
            "alpha": "0.05",
            "bootstrap_samples": 2000,
        },
    }
    base.update(overrides)
    return base


def _ensure_workspace(session) -> None:
    ws = session.get(WorkspaceRecord, CTX.workspace_id)
    if ws is None:
        session.add(WorkspaceRecord(id=CTX.workspace_id, name="LO test"))
        session.flush()


def _seed_asset_versions(session, tag: str, count: int = 2) -> list[str]:
    _ensure_workspace(session)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P lock"))
        session.flush()

    plan_id = f"plan_{tag}"
    if session.get(PlanRecord, plan_id) is None:
        session.add(PlanRecord(
            id=plan_id, workspace_id=CTX.workspace_id,
            product_id=PROD_ID, mode="full", cap=0, total_full=1,
            snapshot_hash="a" * 64,
        ))
        session.flush()

    run_id = f"run_{tag}"
    if session.get(GenerationRunRecord, run_id) is None:
        session.add(GenerationRunRecord(
            id=run_id, workspace_id=CTX.workspace_id,
            product_id=PROD_ID, plan_id=plan_id,
            provider="mock", model="mock", aspect_ratio="1:1",
            image_size="1024", variants=1, use_ref=False,
            status="done", total=1, done=1, failed=0,
            est_cost=Decimal("0"), currency="USD",
        ))
        session.flush()

    av_ids = []
    for i in range(count):
        asset_id = f"asset_{tag}_{i}"
        av_id = f"av_{tag}_{i}"
        if session.get(AssetRecord, asset_id) is None:
            session.add(AssetRecord(
                id=asset_id, workspace_id=CTX.workspace_id,
                product_id=PROD_ID, run_id=run_id,
                combo_id="C01", combo_code="A1", tags_json={},
                filename=f"{tag}_{i}.png", provider="mock", model="mock",
                prompt="test", fingerprint=f"fp_{tag}_{i}", status="ok",
            ))
            session.flush()
        if session.get(AssetVersionRecord, av_id) is None:
            session.add(AssetVersionRecord(
                id=av_id, workspace_id=CTX.workspace_id,
                asset_id=asset_id, kind="generated", aspect_ratio="1:1",
                language="en", object_key=f"{tag}_{i}.png",
                content_sha256=f"{i:0>64x}", byte_size=1, mime_type="image/png",
            ))
            session.flush()
        av_ids.append(av_id)
    govern_asset_versions(
        session, CTX, product_id=PROD_ID, asset_version_ids=av_ids, tag=tag,
    )
    return av_ids


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_lo", "pv_lo", "product"),
        ("le_ov_lo", "ov_lo", "offer"),
    ]:
        if session.get(LibraryEntryRecord, entry_id) is None:
            session.add(LibraryEntryRecord(
                id=entry_id, workspace_id=CTX.workspace_id,
                entry_type=etype, display_name=f"Test {etype}",
            ))
            session.flush()
        if session.get(LibraryVersionRecord, ver_id) is None:
            session.add(LibraryVersionRecord(
                id=ver_id, workspace_id=CTX.workspace_id,
                entry_id=entry_id, entry_type=etype,
                version_no=1, content_json={}, content_hash="c" * 64,
                state="effective", published_by="system",
            ))
            session.flush()
    govern_product_offer_versions(
        session, CTX, product_id=PROD_ID,
        product_version_id="pv_lo", offer_version_id="ov_lo",
    )


def _seed_metrics(session, tag: str, ad_ids: list[str]) -> None:
    acct_id = f"macct_{tag}"
    if session.get(MetaAccountRecord, acct_id) is None:
        session.add(MetaAccountRecord(
            id=acct_id, workspace_id=CTX.workspace_id,
            ad_account_id=f"act_{tag}", currency="USD",
            timezone_name="UTC", api_version="v23.0",
        ))
        session.flush()

    sync_id = f"sync_{tag}"
    if session.get(MetaSyncRunRecord, sync_id) is None:
        session.add(MetaSyncRunRecord(
            id=sync_id, workspace_id=CTX.workspace_id,
            account_id=acct_id, state="completed",
            date_start=date(2026, 7, 1), date_stop=date(2026, 8, 1),
            contract_hash="b" * 64, contract_json={},
            api_version="v23.0", account_currency="USD",
            account_timezone="UTC", idempotency_key=f"sync_{tag}",
        ))
        session.flush()

    for idx, ad_id in enumerate(ad_ids):
        for day_offset in range(7):
            d = date(2026, 7, 25) + timedelta(days=day_offset)
            mid = f"met_{tag}_{idx}_{day_offset}"
            if session.get(AdDailyMetricRecord, mid) is None:
                imp = 1000 * (idx + 1)
                clk = 50 * (idx + 1) + (10 if idx == 0 else 0)
                session.add(AdDailyMetricRecord(
                    id=mid, account_id=acct_id, sync_run_id=sync_id,
                    ad_id=ad_id, metric_date=d,
                    spend=Decimal("10.00"), impressions=imp, clicks=clk,
                    purchases=Decimal("2"), purchase_value=Decimal("40"),
                    currency="USD", contract_hash="b" * 64, raw_json={},
                ))
        session.flush()


class _AddTracker:
    """Wraps session.add to record which model classes are inserted."""

    def __init__(self, real_session, log: list):
        self._real = real_session
        self._log = log

    def add(self, instance):
        cls = type(instance)
        if cls in LOCK_CLASS_MAP:
            self._log.append((cls.__name__, LOCK_CLASS_MAP[cls]))
        return self._real.add(instance)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _tracked_session(log: list):
    """Returns a context-manager factory that wraps tenant_session."""
    @contextmanager
    def wrapper(ctx):
        with tenant_session(ctx) as real_session:
            yield _AddTracker(real_session, log)
    return wrapper


def _class_set(log: list[tuple[str, int]]) -> set[int]:
    return {cls_num for _, cls_num in log}


def _last_index_of(log: list[tuple[str, int]], cls_num: int) -> int:
    """Return the last index in log where cls_num appears, or -1."""
    for i in range(len(log) - 1, -1, -1):
        if log[i][1] == cls_num:
            return i
    return -1


def _first_index_of(log: list[tuple[str, int]], cls_num: int) -> int:
    for i, (_, c) in enumerate(log):
        if c == cls_num:
            return i
    return -1


class LockOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")
        with tenant_session(CTX) as s:
            _ensure_workspace(s)
            if s.get(ProductRecord, PROD_ID) is None:
                s.add(ProductRecord(id=PROD_ID, workspace_id=CTX.workspace_id, name="P lock"))
                s.flush()
            _seed_library_versions(s)

    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()

    def _build_running(self, tag: str) -> tuple[dict, list[str]]:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, tag)
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name=f"LO-{tag}", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        rev = frozen["revision"]
        ad_ids = []
        for cand in frozen["candidates"]:
            ad_id = f"ad_{tag}_{cand['position']}"
            ad_ids.append(ad_id)
            bind_candidate_v2(
                CTX, exp["id"], cand["id"],
                ad_account_id=f"act_{tag}_{cand['position']}",
                ad_id=ad_id,
                expected_revision=rev,
            )
        running = start_experiment_v2(CTX, exp["id"], expected_revision=rev)
        return running, ad_ids

    # --- LO-01: Freeze touches classes 2, 3 and produces state event (1) ---
    def test_LO_01_freeze_class_membership(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lo01")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LO01", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)

        log: list[tuple[str, int]] = []
        from unittest.mock import patch
        with patch("app.experiments.service.tenant_session", _tracked_session(log)):
            freeze_experiment_v2(CTX, exp["id"], expected_revision=3)

        touched = _class_set(log)
        self.assertIn(2, touched, "Freeze must create Version (class 2)")
        self.assertIn(3, touched, "Freeze must create Candidates (class 3)")
        self.assertIn(1, touched, "Freeze must create StateEvent (class 1)")

    # --- LO-02: Start touches class 1 (state event) ---
    def test_LO_02_start_class_membership(self) -> None:
        with tenant_session(CTX) as s:
            avs = _seed_asset_versions(s, "lo02")
        exp = create_experiment_v2(CTX, product_id=PROD_ID, name="LO02", draft_context=_draft_context())
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[0], expected_revision=1)
        add_entry_v2(CTX, exp["id"], asset_version_id=avs[1], expected_revision=2)
        frozen = freeze_experiment_v2(CTX, exp["id"], expected_revision=3)
        rev = frozen["revision"]
        for cand in frozen["candidates"]:
            bind_candidate_v2(
                CTX, exp["id"], cand["id"],
                ad_account_id=f"act_lo02_{cand['position']}",
                ad_id=f"ad_lo02_{cand['position']}",
                expected_revision=rev,
            )

        log: list[tuple[str, int]] = []
        from unittest.mock import patch
        with patch("app.experiments.service.tenant_session", _tracked_session(log)):
            start_experiment_v2(CTX, exp["id"], expected_revision=rev)

        touched = _class_set(log)
        self.assertIn(1, touched, "Start must create StateEvent (class 1)")

    # --- LO-03: Judge creates Judgement (5) and Proposal (5) ---
    def test_LO_03_judge_class_membership(self) -> None:
        running, ad_ids = self._build_running("lo03")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "lo03", ad_ids)

        log: list[tuple[str, int]] = []
        from unittest.mock import patch
        with patch("app.experiments.service.tenant_session", _tracked_session(log)):
            run_judgement_v2(
                CTX, running["id"], expected_revision=running["revision"],
                as_of=date(2026, 8, 1),
            )

        touched = _class_set(log)
        self.assertIn(5, touched, "Judge must create Judgement/Proposal (class 5)")

    # --- LO-04: Confirm creates Decisions (6), Confirmation (6), timeline (7) ---
    def test_LO_04_confirm_class_membership_and_order(self) -> None:
        running, ad_ids = self._build_running("lo04")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "lo04", ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )

        log: list[tuple[str, int]] = []
        from unittest.mock import patch
        with patch("app.experiments.service.tenant_session", _tracked_session(log)):
            confirm_proposal_v2(
                CTX, running["id"], j_result["proposal_id"],
                expected_proposal_sequence=1,
            )

        touched = _class_set(log)
        self.assertIn(6, touched, "Confirm must create Decisions/Confirmation (class 6)")
        self.assertIn(7, touched, "Confirm must create Timeline event (class 7)")

        first_6 = _first_index_of(log, 6)
        first_7 = _first_index_of(log, 7)
        if first_6 >= 0 and first_7 >= 0:
            self.assertLess(first_6, first_7,
                            "Class 6 (Decision/Confirmation) must appear before class 7 (Timeline)")

    # --- LO-05: Timeline (class 7) appears after all domain writes ---
    def test_LO_05_timeline_after_domain_writes(self) -> None:
        running, ad_ids = self._build_running("lo05")
        with tenant_session(CTX) as s:
            _seed_metrics(s, "lo05", ad_ids)
        j_result = run_judgement_v2(
            CTX, running["id"], expected_revision=running["revision"],
            as_of=date(2026, 8, 1),
        )

        log: list[tuple[str, int]] = []
        from unittest.mock import patch
        with patch("app.experiments.service.tenant_session", _tracked_session(log)):
            confirm_proposal_v2(
                CTX, running["id"], j_result["proposal_id"],
                expected_proposal_sequence=1,
            )

        first_7 = _first_index_of(log, 7)
        if first_7 < 0:
            self.fail("No class 7 (Timeline) record found in confirm log")

        for cls_num in (5, 6):
            last_of_cls = _last_index_of(log, cls_num)
            if last_of_cls >= 0:
                self.assertLess(last_of_cls, first_7,
                                f"Class {cls_num} last write at {last_of_cls} must come "
                                f"before class 7 first write at {first_7}")


if __name__ == "__main__":
    unittest.main()
