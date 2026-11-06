"""Contextual experiment conclusion projections (Issue #49 Task 6, spec §0.7).

Covers ``app.generation.experiment_conclusions.project_contextual_conclusions``/
``project_contextual_conclusion_for_context`` directly, plus their wiring
into the gallery's ``contextual_conclusion``/``conclusion`` filter
(``app.generation.asset_queries.list_asset_versions``) and the detail
projection's ``contextual_conclusions[]``
(``app.generation.asset_queries.get_asset_version_detail``).

This repo's test suite uses ``unittest.TestCase`` + ``LibraryFixtures`` +
``TestClient`` with a ``request_tenant_context`` dependency override (see
``tests/test_asset_gallery_page.py`` and ``tests/test_asset_version_detail.py``),
not pytest fixtures like ``db_session``/``tenant_ctx``/``client``/
``auth_headers``/``make_experiment_conclusion`` -- this file follows the
repo's real conventions rather than the plan brief's pytest-flavored
pseudocode.

Two backing sources are exercised (see ``experiment_conclusions.py``'s
module docstring for why):

* the authoritative ``ContextualVerdictProjectionRecord``
  (``contextual_verdict_projections``), which the real
  ``app.experiments.service`` write path always leaves fully anchored
  (``market``/``placement``/``offer_version_id`` are ``NOT NULL``) --
  exercised via ``_seed_authoritative_conclusion``, which builds the real
  FK chain (Experiment -> ExperimentVersion -> two LibraryVersion rows for
  product/offer) a production write would have created;
* the legacy, JSON-anchored ``VerdictDecisionRecord`` (``verdict_decisions``),
  which predates that schema and has no ``asset_version_id``/``market``/
  ``placement`` columns of its own -- exercised via
  ``_seed_legacy_conclusion``, which can produce an incomplete context on
  purpose (the "requires full context" case a real
  ``ContextualVerdictProjectionRecord`` row cannot represent, since its
  ``market``/``placement`` columns are ``NOT NULL``).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

# Import order matters: tests.support.library_fixtures sets
# MARKETAI_DATABASE_URL/MARKETAI_DATA_DIR to an isolated per-process temp dir
# via os.environ.setdefault before app.config is imported anywhere else in
# the process -- see tests/test_v1_library_routes.py for the same note.
from tests.support.library_fixtures import LibraryFixtures

from fastapi.testclient import TestClient

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ContextualVerdictProjectionRecord,
    ExperimentRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    VerdictDecisionRecord,
    new_id,
    utcnow,
)
from app.generation.experiment_conclusions import (
    project_contextual_conclusion_for_context,
    project_contextual_conclusions,
)
from app.main import app
from app.platform.http_identity import request_tenant_context

H64 = "a" * 64


class TestExperimentConclusions(LibraryFixtures):
    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        # experiments.id is a bare (not workspace-scoped) primary key, so a
        # literal id reused across two test methods in this shared-SQLite
        # process would collide on insert -- _seed_authoritative_conclusion
        # always mints a fresh id unless the caller passes one already
        # returned from an earlier call *in the same test*, in which case
        # this cache lets a second AssetVersion be anchored to the exact
        # same Experiment/context (e.g. a Winner and a Loser sharing one
        # experiment context).
        self._experiment_context_cache: dict[str, tuple[str, str, str, str]] = {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_asset_version(self, ctx, session, **overrides) -> AssetVersionRecord:
        product_id = self._product_id(ctx, session)
        plan_id = self._plan_id(ctx, session)
        run = GenerationRunRecord(
            id=new_id("gr"),
            workspace_id=ctx.workspace_id,
            product_id=product_id,
            plan_id=plan_id,
            provider="mock",
            model="mock-v1",
            aspect_ratio="1:1",
            image_size="1024x1024",
            variants=1,
            use_ref=False,
            status="running",
            total=1,
            done=0,
            failed=0,
            est_cost=Decimal("0"),
            currency="USD",
            revision=1,
        )
        session.add(run)
        session.flush()
        asset = AssetRecord(
            id=new_id("asset"),
            workspace_id=ctx.workspace_id,
            product_id=product_id,
            run_id=run.id,
            combo_id="combo_1",
            combo_code="combo_1",
            tags_json={},
            filename="test.png",
            provider="mock",
            model="mock-v1",
            prompt="test prompt",
            fingerprint="fp_" + new_id("h"),
            status="succeeded",
        )
        session.add(asset)
        session.flush()
        fields = dict(
            id=new_id("ver"),
            workspace_id=ctx.workspace_id,
            asset_id=asset.id,
            kind="generated",
            aspect_ratio="1:1",
            object_key=f"legacy/{asset.id}.png",
            content_sha256="c" * 64,
            byte_size=1024,
            mime_type="image/png",
            origin_kind="generation",
            execution_mode="mock",
        )
        fields.update(overrides)
        version = AssetVersionRecord(**fields)
        session.add(version)
        session.flush()
        return version

    def _library_version_id(self, ctx, session) -> str:
        """A bare LibraryVersion row -- just enough to satisfy
        ExperimentVersionRecord's product_version_id/offer_version_id FKs
        (both point at library_versions; reusing one row for both is fine,
        nothing checks entry_type against the FK)."""
        entry = LibraryEntryRecord(
            id=new_id("lentry"), workspace_id=ctx.workspace_id,
            entry_type="product", display_name="Test",
        )
        session.add(entry)
        session.flush()
        version = LibraryVersionRecord(
            id=new_id("lver"), workspace_id=ctx.workspace_id, entry_id=entry.id,
            entry_type="product", version_no=1, content_json={},
            content_hash=H64, published_by="test",
        )
        session.add(version)
        session.flush()
        return version.id

    def _seed_authoritative_conclusion(
        self, ctx, session, *, asset_version_id: str,
        verdict: str = "win", market: str = "US", placement: str = "feed",
        experiment_id: str | None = None, revoked_at=None,
    ) -> str:
        """A real, fully-anchored ``ContextualVerdictProjectionRecord`` row
        plus the FK chain (Experiment -> ExperimentVersion -> LibraryVersion)
        a production write through ``app.experiments.service`` would have
        already created. Returns the ``experiment_id`` -- the opaque
        ``context_id``/``experiment_context`` value (see
        ``experiment_conclusions.py``'s module docstring).

        Passing an ``experiment_id`` already returned from an earlier call
        in this same test reuses that Experiment/ExperimentVersion/
        LibraryVersion chain (its own ``market``/``placement`` win) and
        only adds a new projection row -- the way a real Experiment anchors
        several candidates (e.g. a Winner and a Loser) to one context.
        """
        if experiment_id and experiment_id in self._experiment_context_cache:
            experiment_version_id, library_version_id, market, placement = (
                self._experiment_context_cache[experiment_id]
            )
        else:
            experiment_id = experiment_id or new_id("exp")
            product_id = self._product_id(ctx, session)
            experiment = ExperimentRecord(
                id=experiment_id, workspace_id=ctx.workspace_id, product_id=product_id,
                name="Test experiment", state="frozen", revision=1,
                config_json={}, metric_contract_hash=H64,
            )
            session.add(experiment)
            session.flush()

            library_version_id = self._library_version_id(ctx, session)
            now = utcnow()
            experiment_version = ExperimentVersionRecord(
                id=new_id("ev"), workspace_id=ctx.workspace_id, experiment_id=experiment_id,
                canonicalization_version="p1-migration-v1", frozen_snapshot_json={},
                frozen_snapshot_hash=H64, metric_contract_hash=H64,
                market=market, placement=placement, landing_page_evidence_hash=H64,
                audience_snapshot_json={}, audience_snapshot_hash=H64,
                daily_budget_amount=Decimal("100"), total_budget_cap=Decimal("3000"),
                budget_currency="USD",
                schedule_start_at=now, schedule_end_at=now + timedelta(days=30),
                schedule_timezone="UTC",
                attribution_click_window_days=7, attribution_view_window_days=1,
                statistical_plan_json={}, statistical_plan_hash=H64,
                marketing_vars_schema_version="1",
                product_version_id=library_version_id, offer_version_id=library_version_id,
                candidates_hash=H64, concept_resolutions_hash=H64,
            )
            session.add(experiment_version)
            session.flush()
            experiment_version_id = experiment_version.id
            self._experiment_context_cache[experiment_id] = (
                experiment_version_id, library_version_id, market, placement,
            )

        projection = ContextualVerdictProjectionRecord(
            id=new_id("cvp"), workspace_id=ctx.workspace_id, experiment_id=experiment_id,
            experiment_version_id=experiment_version_id, candidate_id=new_id("cand"),
            decision_id=new_id("dec"), confirmation_id=new_id("conf"),
            verdict=verdict, metric="ctr", estimate=None,
            asset_version_id=asset_version_id,
            product_version_id=library_version_id, offer_version_id=library_version_id,
            market=market, placement=placement, landing_page_evidence_hash=H64,
            confirmed_at=utcnow(), revoked_at=revoked_at,
        )
        session.add(projection)
        session.flush()
        return experiment_id

    def _seed_legacy_conclusion(
        self, ctx, session, *, asset_id: str, context_json: dict, verdict: str = "winner",
    ) -> None:
        session.add(VerdictDecisionRecord(
            id=new_id("vd"), workspace_id=ctx.workspace_id, product_id=self._product_id(ctx, session),
            asset_id=asset_id, verdict=verdict, context_json=context_json, source="legacy",
        ))
        session.flush()

    # ------------------------------------------------------------------
    # project_contextual_conclusions -- authoritative source
    # ------------------------------------------------------------------

    def test_conclusion_returns_winner_for_exact_context(self):
        """Conclusion projection returns winner when anchored to exact context."""
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            version_id = version.id
            experiment_id = self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=version_id,
                verdict="win", market="US", placement="feed",
            )

            conclusions = project_contextual_conclusions(session, self.ctx, version_id)
        self.assertEqual(len(conclusions), 1)
        self.assertEqual(conclusions[0]["verdict"], "winner")
        self.assertEqual(conclusions[0]["context"]["experiment_id"], experiment_id)
        self.assertEqual(conclusions[0]["context"]["context_id"], experiment_id)
        self.assertEqual(conclusions[0]["context"]["market"], "US")
        self.assertEqual(conclusions[0]["context"]["placement"], "feed")
        self.assertIsNone(conclusions[0]["revoked_at"])

    def test_conclusion_excludes_revoked(self):
        """Revoked conclusions are excluded from the projection."""
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            version_id = version.id
            self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=version_id,
                verdict="win", market="US", placement="feed",
                revoked_at=utcnow(),
            )

            conclusions = project_contextual_conclusions(session, self.ctx, version_id)
        self.assertEqual(len(conclusions), 0)

    def test_conclusion_maps_learn_and_lose_verdicts(self):
        """The domain win/learn/lose vocabulary maps to winner/learning/loser."""
        with tenant_session(self.ctx) as session:
            v1 = self._make_asset_version(self.ctx, session)
            v2 = self._make_asset_version(self.ctx, session)
            self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=v1.id, verdict="learn", experiment_id="exp-learn",
            )
            self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=v2.id, verdict="lose", experiment_id="exp-lose",
            )
            v1_verdict = project_contextual_conclusions(session, self.ctx, v1.id)[0]["verdict"]
            v2_verdict = project_contextual_conclusions(session, self.ctx, v2.id)[0]["verdict"]

        self.assertEqual(v1_verdict, "learning")
        self.assertEqual(v2_verdict, "loser")

    # ------------------------------------------------------------------
    # project_contextual_conclusions -- legacy source
    # ------------------------------------------------------------------

    def test_conclusion_requires_full_context(self):
        """Legacy rows without full context are excluded."""
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            version_id = version.id
            self._seed_legacy_conclusion(
                self.ctx, session, asset_id=version.asset_id,
                context_json={
                    "asset_version_id": version_id,
                    "experiment_id": "exp-1",
                    "market": None,  # Missing context
                    "placement": None,
                },
            )

            conclusions = project_contextual_conclusions(session, self.ctx, version_id)
        self.assertEqual(len(conclusions), 0)

    def test_legacy_conclusion_surfaced_when_fully_anchored(self):
        """A legacy row IS surfaced once it carries a complete context."""
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            version_id = version.id
            self._seed_legacy_conclusion(
                self.ctx, session, asset_id=version.asset_id,
                context_json={
                    "asset_version_id": version_id,
                    "experiment_id": "exp-legacy",
                    "market": "DE",
                    "placement": "story",
                },
                verdict="winner",
            )

            conclusions = project_contextual_conclusions(session, self.ctx, version_id)
        self.assertEqual(len(conclusions), 1)
        self.assertEqual(conclusions[0]["verdict"], "winner")
        self.assertEqual(conclusions[0]["context"]["context_id"], "exp-legacy")

    def test_legacy_conclusion_excludes_revoked(self):
        """A legacy row flagged revoked in its context_json is excluded."""
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            version_id = version.id
            self._seed_legacy_conclusion(
                self.ctx, session, asset_id=version.asset_id,
                context_json={
                    "asset_version_id": version_id,
                    "experiment_id": "exp-legacy",
                    "market": "DE",
                    "placement": "story",
                    "revoked": True,
                },
            )

            conclusions = project_contextual_conclusions(session, self.ctx, version_id)
        self.assertEqual(len(conclusions), 0)

    # ------------------------------------------------------------------
    # project_contextual_conclusion_for_context
    # ------------------------------------------------------------------

    def test_conclusion_for_context_returns_verdict(self):
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            version_id = version.id
            experiment_id = self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=version_id, verdict="win",
            )

            matched = project_contextual_conclusion_for_context(
                session, self.ctx, version_id, experiment_id,
            )
            unmatched = project_contextual_conclusion_for_context(
                session, self.ctx, version_id, "some-other-ctx",
            )
        self.assertEqual(matched, "winner")
        self.assertIsNone(unmatched)

    # ------------------------------------------------------------------
    # gallery wiring
    # ------------------------------------------------------------------

    def test_gallery_filter_by_conclusion(self):
        """Gallery conclusion filter works with experiment context."""
        with tenant_session(self.ctx) as session:
            winner = self._make_asset_version(self.ctx, session)
            loser = self._make_asset_version(self.ctx, session)
            experiment_id = self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=winner.id, verdict="win",
            )
            self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=loser.id,
                verdict="lose", experiment_id=experiment_id,
            )
            winner_id = winner.id

        response = self.client.get(
            "/api/v1/generation/assets",
            params={"experiment_context": experiment_id, "conclusion": "winner"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], winner_id)
        for item in items:
            self.assertEqual(item["contextual_conclusion"], "winner")

    def test_gallery_summary_omits_conclusion_without_experiment_context(self):
        """Without experiment_context, contextual_conclusion stays None even
        though a conclusion exists (spec §0.2: never a global property)."""
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=version.id, verdict="win",
            )
            version_id = version.id

        response = self.client.get("/api/v1/generation/assets")
        self.assertEqual(response.status_code, 200, response.text)
        items = {item["id"]: item for item in response.json()["items"]}
        self.assertIsNone(items[version_id]["contextual_conclusion"])

    def test_gallery_conclusion_none_filter_matches_unconfirmed(self):
        """conclusion=none matches versions with no confirmed conclusion in
        the given context."""
        with tenant_session(self.ctx) as session:
            winner = self._make_asset_version(self.ctx, session)
            plain = self._make_asset_version(self.ctx, session)
            experiment_id = self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=winner.id, verdict="win",
            )
            plain_id = plain.id

        response = self.client.get(
            "/api/v1/generation/assets",
            params={"experiment_context": experiment_id, "conclusion": "none"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], plain_id)

    # ------------------------------------------------------------------
    # detail wiring
    # ------------------------------------------------------------------

    def test_detail_includes_contextual_conclusions(self):
        """Detail view lists all conclusions with their full context."""
        with tenant_session(self.ctx) as session:
            version = self._make_asset_version(self.ctx, session)
            version_id = version.id
            experiment_id = self._seed_authoritative_conclusion(
                self.ctx, session, asset_version_id=version_id,
                verdict="win", market="US", placement="feed",
            )

        response = self.client.get(f"/api/v1/generation/asset-versions/{version_id}")
        self.assertEqual(response.status_code, 200, response.text)
        conclusions = response.json()["contextual_conclusions"]
        self.assertEqual(len(conclusions), 1)
        self.assertEqual(conclusions[0]["verdict"], "winner")
        self.assertEqual(conclusions[0]["asset_version_id"], version_id)
        self.assertEqual(conclusions[0]["context"]["experiment_id"], experiment_id)
        self.assertEqual(conclusions[0]["context"]["market"], "US")
        self.assertEqual(conclusions[0]["context"]["placement"], "feed")
