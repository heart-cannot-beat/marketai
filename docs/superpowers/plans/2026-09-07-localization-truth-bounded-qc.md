# Localization Truth, Bounded QC, and Asset Lineage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the full Localization feature area: truth resolution, bounded QC, asset lineage, and nine-market support — Issue #24.

**Architecture:** 32 new tables and 2 existing table evolutions in a single Alembic revision. Nine domain modules under `app/localization/` and a generalized provider dispatch under `app/providers/`. Two fact-source adapters (Manual, Controlled Mock). Three-phase external-call coordination. Event-sourced eligibility, CAS projections, append-only evidence, and composite FK integrity. PostgreSQL RLS + FORCE RLS for production tenancy; SQLite compatibility path for fast tests.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x (`Mapped[T]` / `mapped_column`), Alembic, pytest, PostgreSQL 16, SQLite, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md`

## Global Constraints

- All IDs: `new_id("prefix")` → `"prefix_{uuid4hex}"`, `String(64)`.
- Every workspace-scoped table: `workspace_id` FK → `workspaces.id` + `UNIQUE(workspace_id, id)`.
- All composite FKs include `workspace_id` — no cross-Workspace reference possible even without RLS.
- Platform-scoped tables (market policies): no `workspace_id`, shared across all workspaces.
- Timestamps: `DateTime(timezone=True)`, `default=utcnow`.
- Enum columns: `String(20-40)` + `CheckConstraint` with explicit `name`.
- CAS: `revision: Integer` column, `WHERE revision = :expected` in UPDATE.
- Immutable tables: PostgreSQL trigger rejects UPDATE/DELETE on all columns except projections.
- Append-only tables: PostgreSQL trigger rejects UPDATE/DELETE entirely.
- ORM records use `class XRecord(Base)` with `__tablename__`, all in `app/adapters/persistence/models.py`.
- Evidence tier: `min(source_gen_modes, experiment_evidence_modes, truth_data_mode, provider_exec_mode)` after normalization.
- No Shopify/Claim/Shipping stub adapters — only Manual and Controlled Mock.
- No QC waiver — adoption requires completing all frozen QC checks.
- One Alembic revision, DDL ordered per §1.11: platform → truth → existing evolutions → batch → claim/copy → item/attempt → binding → triggers/RLS.
- Naming: `fk_{source}_{cols}_{target}`, `uq_{table}_{cols}`, `ck_{table}_{field}`.
- Migration: `_is_pg(conn)` dialect guard for PostgreSQL-only DDL.

---

### Task 1: Platform ORM Records and Nine-Market Seed

**Files:**
- Modify: `app/adapters/persistence/models.py` (append after existing records)
- Create: `tests/test_localization_platform.py`

**Interfaces:**
- Consumes: `Base`, `new_id`, `utcnow`, `String`, `Integer`, `DateTime`, `JSON`, `CheckConstraint`, `UniqueConstraint`, `ForeignKeyConstraint`, `Mapped`, `mapped_column` from `models.py`
- Produces: `MarketPolicyRecord`, `MarketPolicyVersionRecord`, `MarketPolicyVersionEventRecord`

- [ ] **Step 1: Write failing test for MarketPolicyRecord import**

```python
# tests/test_localization_platform.py
import unittest
from app.adapters.persistence.models import (
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    MarketPolicyVersionEventRecord,
    new_id, utcnow,
)


class TestMarketPolicyRecords(unittest.TestCase):
    def test_market_policy_table_name(self):
        self.assertEqual(MarketPolicyRecord.__tablename__, "market_policies")

    def test_market_policy_version_table_name(self):
        self.assertEqual(MarketPolicyVersionRecord.__tablename__, "market_policy_versions")

    def test_market_policy_version_event_table_name(self):
        self.assertEqual(MarketPolicyVersionEventRecord.__tablename__, "market_policy_version_events")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_localization_platform.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement MarketPolicyRecord**

Append to `app/adapters/persistence/models.py`:

```python
# ---------------------------------------------------------------------------
# Localization — Platform-scoped tables (Issue #24, spec §1.2)
# ---------------------------------------------------------------------------


class MarketPolicyRecord(Base):
    __tablename__ = "market_policies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_id: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
```

- [ ] **Step 4: Implement MarketPolicyVersionRecord**

```python
class MarketPolicyVersionRecord(Base):
    __tablename__ = "market_policy_versions"
    __table_args__ = (
        UniqueConstraint("market_policy_id", "version_number",
                         name="uq_mpv_policy_version"),
        UniqueConstraint("id", "market_policy_id",
                         name="uq_mpv_id_policy"),
        CheckConstraint(
            "version_state IN ('effective', 'superseded')",
            name="ck_mpv_version_state",
        ),
        CheckConstraint(
            "shipping_address_granularity IN ('country', 'region', 'postal_code')",
            name="ck_mpv_shipping_granularity",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_policy_id: Mapped[str] = mapped_column(
        ForeignKey("market_policies.id"), nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_state: Mapped[str] = mapped_column(String(20), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    pricing_context_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    digit_format_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    base_tone: Mapped[str] = mapped_column(String(40), nullable=False)
    reserved_words_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    restricted_claim_types_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    requires_local_copy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    shipping_address_granularity: Mapped[str] = mapped_column(String(20), nullable=False)
    max_fact_staleness_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    qc_rules_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
```

- [ ] **Step 5: Implement MarketPolicyVersionEventRecord**

```python
class MarketPolicyVersionEventRecord(Base):
    __tablename__ = "market_policy_version_events"
    __table_args__ = (
        UniqueConstraint("market_policy_id", "sequence",
                         name="uq_mpve_policy_sequence"),
        UniqueConstraint("market_policy_version_id", "event_type",
                         name="uq_mpve_version_event_type"),
        UniqueConstraint("deduplication_key",
                         name="uq_mpve_dedup_key"),
        UniqueConstraint("id", "market_policy_version_id",
                         name="uq_mpve_id_version"),
        ForeignKeyConstraint(
            ["market_policy_version_id", "market_policy_id"],
            ["market_policy_versions.id", "market_policy_versions.market_policy_id"],
            name="fk_mpve_version_policy_mpv",
        ),
        CheckConstraint(
            "event_type IN ('published', 'superseded')",
            name="ck_mpve_event_type",
        ),
        CheckConstraint(
            "current_state IN ('effective', 'superseded')",
            name="ck_mpve_current_state",
        ),
        CheckConstraint("sequence >= 1", name="ck_mpve_sequence_positive"),
        CheckConstraint(
            "(event_type = 'published' AND previous_state IS NULL "
            "AND current_state = 'effective') OR "
            "(event_type = 'superseded' AND previous_state = 'effective' "
            "AND current_state = 'superseded')",
            name="ck_mpve_state_consistency",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_policy_id: Mapped[str] = mapped_column(
        ForeignKey("market_policies.id"), nullable=False,
    )
    market_policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("market_policy_versions.id"), nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_state: Mapped[str] = mapped_column(String(20), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_localization_platform.py -v`
Expected: PASS

- [ ] **Step 7: Write nine-market seed data test**

Add to `tests/test_localization_platform.py`:

```python
BETA_MARKETS = {
    "en-GB": ("en-GB", "GB", "GBP"),
    "en-AU": ("en-AU", "AU", "AUD"),
    "en-CA": ("en-CA", "CA", "CAD"),
    "de-DE": ("de-DE", "DE", "EUR"),
    "fr-FR": ("fr-FR", "FR", "EUR"),
    "es-ES": ("es-ES", "ES", "EUR"),
    "it-IT": ("it-IT", "IT", "EUR"),
    "ja-JP": ("ja-JP", "JP", "JPY"),
    "ko-KR": ("ko-KR", "KR", "KRW"),
}


class TestNineMarketSeed(unittest.TestCase):
    def test_seed_creates_nine_policies(self):
        from app.adapters.persistence.database import engine, platform_session
        from app.localization.platform import seed_beta_market_catalog

        with platform_session() as session:
            seed_beta_market_catalog(session)
            policies = session.query(MarketPolicyRecord).all()
            self.assertEqual(len(policies), 9)
            market_ids = {p.market_id for p in policies}
            self.assertEqual(market_ids, set(BETA_MARKETS.keys()))
            for p in policies:
                locale, country, currency = BETA_MARKETS[p.market_id]
                self.assertEqual(p.locale, locale)
                self.assertEqual(p.country, country)
                self.assertEqual(p.currency, currency)
```

- [ ] **Step 8: Create platform seed module**

```python
# app/localization/platform.py
from __future__ import annotations

from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    MarketPolicyVersionEventRecord,
    new_id, utcnow,
)

_BETA_CATALOG = (
    ("en-GB", "en-GB", "GB", "GBP"),
    ("en-AU", "en-AU", "AU", "AUD"),
    ("en-CA", "en-CA", "CA", "CAD"),
    ("de-DE", "de-DE", "DE", "EUR"),
    ("fr-FR", "fr-FR", "FR", "EUR"),
    ("es-ES", "es-ES", "ES", "EUR"),
    ("it-IT", "it-IT", "IT", "EUR"),
    ("ja-JP", "ja-JP", "JP", "JPY"),
    ("ko-KR", "ko-KR", "KR", "KRW"),
)


def seed_beta_market_catalog(session: Session) -> list[MarketPolicyRecord]:
    """Idempotent seed for the nine v9 Beta markets."""
    existing = {
        r.market_id
        for r in session.query(MarketPolicyRecord).all()
    }
    policies = []
    now = utcnow()
    for market_id, locale, country, currency in _BETA_CATALOG:
        if market_id in existing:
            continue
        policy = MarketPolicyRecord(
            id=new_id("mp"),
            market_id=market_id,
            locale=locale,
            country=country,
            currency=currency,
            created_at=now,
        )
        session.add(policy)
        session.flush()

        version = MarketPolicyVersionRecord(
            id=new_id("mpv"),
            market_policy_id=policy.id,
            version_number=1,
            version_state="effective",
            locale=locale,
            country=country,
            currency=currency,
            pricing_context_json={},
            digit_format_json=_default_digit_format(locale),
            base_tone="neutral",
            reserved_words_json=[],
            restricted_claim_types_json=[],
            requires_local_copy=locale[:2] not in ("en",),
            shipping_address_granularity="country",
            max_fact_staleness_seconds=86400,
            qc_rules_json={},
            created_at=now,
        )
        session.add(version)
        session.flush()

        event = MarketPolicyVersionEventRecord(
            id=new_id("mpve"),
            market_policy_id=policy.id,
            market_policy_version_id=version.id,
            sequence=1,
            event_type="published",
            previous_state=None,
            current_state="effective",
            deduplication_key=f"mpve-{version.id}-published",
            actor_id="system",
            created_at=now,
        )
        session.add(event)
        session.flush()
        policies.append(policy)
    return policies


def _default_digit_format(locale: str) -> dict:
    if locale.startswith("de") or locale.startswith("fr") or locale.startswith("es") or locale.startswith("it"):
        return {"decimal_separator": ",", "grouping_separator": ".", "currency_position": "suffix"}
    if locale.startswith("ja") or locale.startswith("ko"):
        return {"decimal_separator": ".", "grouping_separator": ",", "currency_position": "prefix"}
    return {"decimal_separator": ".", "grouping_separator": ",", "currency_position": "prefix"}
```

- [ ] **Step 9: Run full test suite and commit**

Run: `python -m pytest tests/test_localization_platform.py -v`

```bash
git add app/adapters/persistence/models.py app/localization/__init__.py app/localization/platform.py tests/test_localization_platform.py
git commit -m "feat(localization): add platform ORM records and nine-market seed (#24)"
```

**Acceptance tests:** §3I (migration metadata), §16.3 (per-country market context — all nine markets use correct locale/country/currency).

---

### Task 2: Truth Resolution ORM Records

**Files:**
- Modify: `app/adapters/persistence/models.py` (append after Task 1 records)
- Create: `tests/test_localization_truth_records.py`

**Interfaces:**
- Consumes: `Base`, `new_id`, `utcnow`, `MarketPolicyVersionRecord` from Task 1
- Produces: `TruthPackageRecord`, `TruthPackageMarketRecord`, `TruthSourceAttemptRecord`, `TruthFactRecord`, `TruthDerivedFactRecord`, `TruthDerivedFactInputRecord`, `FactLibraryDependencyRecord`, `FactMarketPolicyDependencyRecord`

- [ ] **Step 1: Write failing import test**

```python
# tests/test_localization_truth_records.py
import unittest
from app.adapters.persistence.models import (
    TruthPackageRecord,
    TruthPackageMarketRecord,
    TruthSourceAttemptRecord,
    TruthFactRecord,
    TruthDerivedFactRecord,
    TruthDerivedFactInputRecord,
    FactLibraryDependencyRecord,
    FactMarketPolicyDependencyRecord,
)


class TestTruthRecordTableNames(unittest.TestCase):
    def test_table_names(self):
        expected = {
            TruthPackageRecord: "truth_packages",
            TruthPackageMarketRecord: "truth_package_markets",
            TruthSourceAttemptRecord: "truth_source_attempts",
            TruthFactRecord: "truth_facts",
            TruthDerivedFactRecord: "truth_derived_facts",
            TruthDerivedFactInputRecord: "truth_derived_fact_inputs",
            FactLibraryDependencyRecord: "fact_library_dependencies",
            FactMarketPolicyDependencyRecord: "fact_market_policy_dependencies",
        }
        for cls, name in expected.items():
            self.assertEqual(cls.__tablename__, name, f"{cls.__name__}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_localization_truth_records.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement TruthPackageRecord**

Append to `app/adapters/persistence/models.py`:

```python
# ---------------------------------------------------------------------------
# Localization — Truth resolution tables (Issue #24, spec §1.3)
# ---------------------------------------------------------------------------


class TruthPackageRecord(Base):
    __tablename__ = "truth_packages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_tp_workspace_id"),
        UniqueConstraint("workspace_id", "id", "truth_data_mode",
                         name="uq_tp_workspace_id_mode"),
        UniqueConstraint("workspace_id", "canonical_hash",
                         name="uq_tp_workspace_hash"),
        ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tp_product_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tp_offer_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "truth_resolution_policy_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tp_truth_resolution_policy_version",
        ),
        CheckConstraint(
            "truth_data_mode IN ('real', 'mock')",
            name="ck_tp_truth_data_mode",
        ),
        CheckConstraint(
            "status IN ('full', 'degraded', 'failed')",
            name="ck_tp_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    product_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    truth_resolution_policy_version_id: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    truth_data_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
    )
```

- [ ] **Step 4: Implement remaining truth tables**

Implement `TruthPackageMarketRecord`, `TruthSourceAttemptRecord`, `TruthFactRecord`, `TruthDerivedFactRecord`, `TruthDerivedFactInputRecord`, `FactLibraryDependencyRecord`, `FactMarketPolicyDependencyRecord` following the exact column and constraint definitions from spec §1.3. Each record follows the same pattern: `class XRecord(Base)` with `__tablename__`, `__table_args__` containing all `UniqueConstraint`, `ForeignKeyConstraint`, and `CheckConstraint` entries from the spec, and all `Mapped[T]` columns.

Key constraints to implement exactly (do NOT simplify):
- `TruthSourceAttemptRecord`: five conditional CHECKs for outcome/failure_code/skip_code/evidence_hash/mock fields, composite FK to `truth_package_markets` with parent-carrying `truth_package_id`
- `TruthFactRecord`: five UNIQUE constraints for progressively wider parent-carrying keys, composite FK to `truth_source_attempts` with full `(workspace_id, accepted_source_attempt_id, truth_package_id, truth_package_market_id, fact_type, scope_key)`
- `FactLibraryDependencyRecord`: two partial UNIQUEs (one for `truth_fact_id IS NOT NULL`, one for `truth_fact_id IS NULL`), CHECK that maps nullable `truth_fact_id` to dependency_kind categories
- `FactMarketPolicyDependencyRecord`: parent-carrying composite FK through `truth_package_markets` to prove market/policy alignment

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_localization_truth_records.py -v`

```bash
git add app/adapters/persistence/models.py tests/test_localization_truth_records.py
git commit -m "feat(localization): add truth resolution ORM records (#24)"
```

**Acceptance tests:** §3D (truth source attempt persistence), §3I (composite FK integrity).

---

### Task 3: Existing Table Evolution

**Files:**
- Modify: `app/adapters/persistence/models.py` (edit `AssetVersionRecord.__table_args__`)
- Modify: `app/entitlements/cost_models.py` (edit `ProviderCallAttemptRecord`)
- Create: `tests/test_localization_table_evolution.py`

**Interfaces:**
- Consumes: `AssetVersionRecord`, `ProviderCallAttemptRecord`, `TruthPackageRecord` from Tasks 1-2
- Produces: evolved `AssetVersionRecord` (origin_kind gains `'localized'`, new truth FK/CHECK/UNIQUE), evolved `ProviderCallAttemptRecord` (wider step_kind, nullable spend_reservation, chargeability, truth_package_id)

- [ ] **Step 1: Write failing test for new origin_kind value**

```python
# tests/test_localization_table_evolution.py
import unittest
from app.adapters.persistence.models import AssetVersionRecord


class TestAssetVersionEvolution(unittest.TestCase):
    def test_origin_kind_check_includes_localized(self):
        for c in AssetVersionRecord.__table_args__:
            if hasattr(c, "name") and c.name == "ck_asset_version_origin_kind":
                self.assertIn("localized", str(c.sqltext))
                return
        self.fail("ck_asset_version_origin_kind not found")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_localization_table_evolution.py::TestAssetVersionEvolution -v`
Expected: FAIL (current CHECK does not include 'localized')

- [ ] **Step 3: Update AssetVersionRecord origin_kind CHECK**

In `app/adapters/persistence/models.py`, change the `ck_asset_version_origin_kind` constraint:

```python
CheckConstraint(
    "origin_kind IN ('generation', 'upload', 'reuse', 'legacy', 'localized')",
    name="ck_asset_version_origin_kind",
),
```

Add new constraints to `AssetVersionRecord.__table_args__`:

```python
UniqueConstraint(
    "workspace_id", "id", "truth_package_id", "parent_version_id",
    name="uq_av_workspace_truth_parent",
),
CheckConstraint(
    "origin_kind != 'localized' OR "
    "(truth_package_id IS NOT NULL AND parent_version_id IS NOT NULL)",
    name="ck_av_localized_requires_truth_parent",
),
```

- [ ] **Step 4: Write failing test for ProviderCallAttemptRecord evolution**

```python
class TestProviderCallAttemptEvolution(unittest.TestCase):
    def test_step_kind_check_includes_localization_values(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord
        for c in ProviderCallAttemptRecord.__table_args__:
            if hasattr(c, "name") and c.name == "ck_provider_call_attempt_step_kind":
                text = str(c.sqltext)
                for val in ("source_analysis", "translation", "localization_edit",
                            "localization_ocr", "localization_visual_qc"):
                    self.assertIn(val, text)
                return
        self.fail("ck_provider_call_attempt_step_kind not found")

    def test_chargeability_column_exists(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord
        self.assertTrue(hasattr(ProviderCallAttemptRecord, "chargeability"))

    def test_truth_package_id_column_exists(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord
        self.assertTrue(hasattr(ProviderCallAttemptRecord, "truth_package_id"))

    def test_spend_reservation_id_nullable(self):
        from app.entitlements.cost_models import ProviderCallAttemptRecord
        col = ProviderCallAttemptRecord.__table__.columns["spend_reservation_id"]
        self.assertTrue(col.nullable)
```

- [ ] **Step 5: Update ProviderCallAttemptRecord**

In `app/entitlements/cost_models.py`:

1. Change `step_kind` column type from `String(20)` to `String(40)`.
2. Update step_kind CHECK to include all seven values.
3. Change `spend_reservation_id` to `nullable=True`.
4. Add `chargeability` column:
```python
chargeability: Mapped[str] = mapped_column(
    String(24), nullable=False, default="charged",
)
```
5. Add `truth_package_id` column:
```python
truth_package_id: Mapped[str | None] = mapped_column(
    String(64), nullable=True,
)
```
6. Add chargeability CHECK, truth-required CHECK, and alternate key UNIQUE to `__table_args__`.
7. Add composite FK `(workspace_id, truth_package_id)` → `truth_packages(workspace_id, id)`.

- [ ] **Step 6: Run tests and commit**

Run: `python -m pytest tests/test_localization_table_evolution.py -v`

```bash
git add app/adapters/persistence/models.py app/entitlements/cost_models.py tests/test_localization_table_evolution.py
git commit -m "feat(localization): evolve asset_versions and provider_call_attempts (#24)"
```

**Acceptance tests:** §3I (existing rows survive, wider step kinds, chargeability CHECK).

---

### Task 4: BrandLocalizationPolicy Library Type

**Files:**
- Modify: `app/library/contracts.py`
- Modify: `tests/test_library_core.py` (or create `tests/test_library_brand_policy.py`)

**Interfaces:**
- Consumes: `ENTRY_TYPES`, `OWNER_ONLY_PUBLISH`, `WITHDRAWAL_DOWNSTREAM_EVENT_TYPES` from `contracts.py`
- Produces: `ENTRY_BRAND_LOCALIZATION_POLICY` constant; updated `ENTRY_TYPES` and `OWNER_ONLY_PUBLISH`; content validator; canonical hashing; dependency extraction

- [ ] **Step 1: Write failing test for new entry type**

```python
# tests/test_library_brand_policy.py
import unittest
from app.library.contracts import (
    ENTRY_TYPES, OWNER_ONLY_PUBLISH,
    ENTRY_BRAND_LOCALIZATION_POLICY,
)


class TestBrandLocalizationPolicyType(unittest.TestCase):
    def test_constant_value(self):
        self.assertEqual(ENTRY_BRAND_LOCALIZATION_POLICY, "brand_localization_policy")

    def test_registered_in_entry_types(self):
        self.assertIn(ENTRY_BRAND_LOCALIZATION_POLICY, ENTRY_TYPES)

    def test_owner_only_publish(self):
        self.assertIn(ENTRY_BRAND_LOCALIZATION_POLICY, OWNER_ONLY_PUBLISH)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_library_brand_policy.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Add ENTRY_BRAND_LOCALIZATION_POLICY constant**

In `app/library/contracts.py`:

After `ENTRY_TRUTH_RESOLUTION_POLICY = "truth_resolution_policy"`, add:
```python
ENTRY_BRAND_LOCALIZATION_POLICY = "brand_localization_policy"
```

Add to `ENTRY_TYPES` frozenset and to `OWNER_ONLY_PUBLISH` frozenset.

- [ ] **Step 4: Add content validator**

In `app/library/contracts.py`, add a validator function following the existing pattern (see `_validate_data_source_content`, `_validate_manual_fact_set_content`):

```python
def _validate_brand_localization_policy_content(
    content: dict, *, version: int,
) -> list[Finding]:
    findings: list[Finding] = []
    for required in ("claim_switches", "style_guide"):
        if required not in content:
            findings.append(Finding(
                code=f"brand_policy.missing_{required}",
                message=f"Required field '{required}' missing",
                path=required,
            ))
    if "claim_switches" in content and not isinstance(content["claim_switches"], dict):
        findings.append(Finding(
            code="brand_policy.invalid_claim_switches",
            message="claim_switches must be an object",
            path="claim_switches",
        ))
    return findings
```

Register it in the `CONTENT_VALIDATORS` dispatch (or equivalent pattern used by the existing validators).

- [ ] **Step 5: Add canonical hashing and dependency extraction**

Follow the existing pattern for other entry types. The policy's dependencies are any referenced Library versions (e.g., base terminology sets). Add to `extract_dependencies()` dispatch.

- [ ] **Step 6: Run tests and commit**

Run: `python -m pytest tests/test_library_brand_policy.py -v && python -m pytest tests/test_library_core.py -v`

```bash
git add app/library/contracts.py tests/test_library_brand_policy.py
git commit -m "feat(library): register BrandLocalizationPolicy entry type (#24)"
```

**Acceptance tests:** §16.13 (Owner-only publish/withdraw with MFA step-up).

---

### Task 5: Localization Domain ORM Records — Batch, Claim, Copy

**Files:**
- Modify: `app/adapters/persistence/models.py`
- Create: `tests/test_localization_domain_records.py`

**Interfaces:**
- Consumes: all records from Tasks 1-3
- Produces: `SourceClaimInventoryRecord`, `SourceClaimConfirmationRecord`, `LocalizationClaimDecisionSetRecord`, `LocalizationClaimDecisionRecord`, `LocalizationBatchRecord`, `LocalizationBatchSourceRecord`, `LocalizationBatchCommandRecord`, `LocalizationBatchMarketTargetRecord`, `LocalizedCopyVersionRecord`, `LocalizedCopyApprovalRecord`

- [ ] **Step 1: Write failing import test**

```python
# tests/test_localization_domain_records.py
import unittest
from app.adapters.persistence.models import (
    SourceClaimInventoryRecord,
    SourceClaimConfirmationRecord,
    LocalizationClaimDecisionSetRecord,
    LocalizationClaimDecisionRecord,
    LocalizationBatchRecord,
    LocalizationBatchSourceRecord,
    LocalizationBatchCommandRecord,
    LocalizationBatchMarketTargetRecord,
    LocalizedCopyVersionRecord,
    LocalizedCopyApprovalRecord,
)


class TestDomainRecordTableNames(unittest.TestCase):
    def test_table_names(self):
        expected = {
            SourceClaimInventoryRecord: "source_claim_inventories",
            SourceClaimConfirmationRecord: "source_claim_confirmations",
            LocalizationClaimDecisionSetRecord: "localization_claim_decision_sets",
            LocalizationClaimDecisionRecord: "localization_claim_decisions",
            LocalizationBatchRecord: "localization_batches",
            LocalizationBatchSourceRecord: "localization_batch_sources",
            LocalizationBatchCommandRecord: "localization_batch_commands",
            LocalizationBatchMarketTargetRecord: "localization_batch_market_targets",
            LocalizedCopyVersionRecord: "localized_copy_versions",
            LocalizedCopyApprovalRecord: "localized_copy_approvals",
        }
        for cls, name in expected.items():
            self.assertEqual(cls.__tablename__, name, f"{cls.__name__}")
```

- [ ] **Step 2: Implement all 10 records**

Implement each record following spec §1.4-§1.6 exactly. Every column, every `UniqueConstraint`, every `ForeignKeyConstraint`, every `CheckConstraint` from the spec. Key records to implement carefully:

- `LocalizationBatchRecord`: 8 UNIQUE constraints for different provenance contexts, CAS revision, frozen-field protection trigger, phase/preparation_stage/outcome CHECKs
- `LocalizationBatchSourceRecord`: 5 UNIQUE constraints including full Lineage provenance target, composite FK to `experiment_decisions` with parent-carrying key
- `LocalizationClaimDecisionSetRecord`: composite FK through `truth_package_markets` and `source_claim_inventories`, decision_count CHECK
- `LocalizationClaimDecisionRecord`: three action-specific CHECK constraints for replace/remove/preserve, composite FK for replacement_fact through `truth_facts`
- `LocalizedCopyVersionRecord`: copy_origin CHECK distinguishing translated vs source_locale_projection, translation field CHECKs

- [ ] **Step 3: Run tests and commit**

Run: `python -m pytest tests/test_localization_domain_records.py -v`

```bash
git add app/adapters/persistence/models.py tests/test_localization_domain_records.py
git commit -m "feat(localization): add batch, claim, copy domain ORM records (#24)"
```

**Acceptance tests:** §3I (composite FK integrity, partial unique indexes).

---

### Task 6: Localization Domain ORM Records — Item, Attempt, QC, Provider, Binding

**Files:**
- Modify: `app/adapters/persistence/models.py`
- Modify: `tests/test_localization_domain_records.py`

**Interfaces:**
- Consumes: all records from Tasks 1-5
- Produces: `LocalizationItemRecord`, `LocalizationAttemptRecord`, `LocalizationAttemptOutputRecord`, `LocalizationOutputPublicationRecord`, `LocalizationQcRecordRecord`, `LocalizationItemProviderCallRecord`, `LocalizationPreparationProviderCallRecord`, `LocalizationSemanticClaimRecord`, `MarketAssetBindingRecord`, `MarketAssetBindingEventRecord`, `LocalizedAssetLineageRecord`

- [ ] **Step 1: Write failing import test**

Add to `tests/test_localization_domain_records.py`:

```python
from app.adapters.persistence.models import (
    LocalizationItemRecord,
    LocalizationAttemptRecord,
    LocalizationAttemptOutputRecord,
    LocalizationOutputPublicationRecord,
    LocalizationQcRecordRecord,
    LocalizationItemProviderCallRecord,
    LocalizationPreparationProviderCallRecord,
    LocalizationSemanticClaimRecord,
    MarketAssetBindingRecord,
    MarketAssetBindingEventRecord,
    LocalizedAssetLineageRecord,
)


class TestItemAttemptBindingRecordTableNames(unittest.TestCase):
    def test_table_names(self):
        expected = {
            LocalizationItemRecord: "localization_items",
            LocalizationAttemptRecord: "localization_attempts",
            LocalizationAttemptOutputRecord: "localization_attempt_outputs",
            LocalizationOutputPublicationRecord: "localization_output_publications",
            LocalizationQcRecordRecord: "localization_qc_records",
            LocalizationItemProviderCallRecord: "localization_item_provider_calls",
            LocalizationPreparationProviderCallRecord: "localization_preparation_provider_calls",
            LocalizationSemanticClaimRecord: "localization_semantic_claims",
            MarketAssetBindingRecord: "market_asset_bindings",
            MarketAssetBindingEventRecord: "market_asset_binding_events",
            LocalizedAssetLineageRecord: "localized_asset_lineages",
        }
        for cls, name in expected.items():
            self.assertEqual(cls.__tablename__, name, f"{cls.__name__}")
```

- [ ] **Step 2: Implement all 11 records**

Implement each record following spec §1.7-§1.10 exactly. Key records:

- `LocalizationItemRecord`: 6 UNIQUE constraints, composite FKs to batch_sources, batch_market_targets, truth_package_markets, claim_decision_sets, and copy_versions — all with full parent-carrying columns. CAS revision. State CHECK with 9 values.
- `LocalizationAttemptRecord`: parent-carrying key `(workspace_id, id, item_id, batch_id)`, composite FK to items. State transition rules (authorized → dispatching → completed|failed|uncertain, uncertain → completed|failed with evidence).
- `LocalizationAttemptOutputRecord`: single-output guarantee via `UNIQUE(workspace_id, attempt_id)`, parent-carrying FK to attempts.
- `LocalizationOutputPublicationRecord`: `UNIQUE(workspace_id, output_id)` for single-use guarantee, `UNIQUE(workspace_id, asset_version_id)`, publication_kind CHECK.
- `LocalizationPreparationProviderCallRecord`: work_kind/provider_step_kind CHECK, write-once result IDs, CAS state transitions.
- `LocalizationSemanticClaimRecord`: `UNIQUE(workspace_id, item_fingerprint_hash)` for global cross-batch idempotency.
- `LocalizedAssetLineageRecord`: composite FKs to batches (8-column), batch_sources (7-column), items (13-column), attempts, outputs, publications, truth_package_markets, claim_decision_sets, copy_versions, asset_versions. All exact provenance modes.
- `MarketAssetBindingRecord`: partial unique `WHERE active = true`, CAS revision, composite FK to items with full provenance context.

- [ ] **Step 3: Run tests and commit**

Run: `python -m pytest tests/test_localization_domain_records.py -v`

```bash
git add app/adapters/persistence/models.py tests/test_localization_domain_records.py
git commit -m "feat(localization): add item, attempt, QC, provider, binding ORM records (#24)"
```

**Acceptance tests:** §3I (full parent-carrying FK chain from Batch → Item → Attempt → Output → Publication → Lineage).

---

### Task 7: Alembic Migration Revision

**Files:**
- Create: `migrations/versions/u1a2b3c4d5e6_localization_truth_bounded_qc.py`
- Create: `tests/test_localization_migration.py`

**Interfaces:**
- Consumes: all ORM records from Tasks 1-6
- Produces: complete database schema for all 32 new tables + 2 existing evolutions, PostgreSQL triggers, RLS, grants

- [ ] **Step 1: Write migration test**

```python
# tests/test_localization_migration.py
import unittest
from alembic import command
from alembic.config import Config


class TestLocalizationMigration(unittest.TestCase):
    def test_upgrade_from_head(self):
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")

    def test_downgrade_and_upgrade(self):
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "t1a2b3c4d5e6")
        command.upgrade(cfg, "head")
```

- [ ] **Step 2: Create migration file**

```python
# migrations/versions/u1a2b3c4d5e6_localization_truth_bounded_qc.py
"""Localization truth, bounded QC, and asset lineage (Issue #24).

DDL order per spec §1.11:
  1. MarketPolicy tables + events
  2. TruthPackage/Fact/dependency tables
  3. Preflight + asset_versions evolution + provider_call_attempts evolution
  4. Batch core, source/target, commands (deferred Item FK)
  5. Inventories, confirmations, ClaimDecisionSets/decisions, CopyVersions/approvals
  6. Items, deferred FK, Attempts, outputs, publications, QC, semantic claims, provider joins
  7. Bindings, binding events, lineage
  8. PostgreSQL triggers, RLS, grants, metadata assertions

Revision ID: u1a2b3c4d5e6
Revises: t1a2b3c4d5e6
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "u1a2b3c4d5e6"
down_revision = "t1a2b3c4d5e6"
branch_labels = None
depends_on = None


def _is_pg(conn) -> bool:
    return conn.dialect.name == "postgresql"


def upgrade() -> None:
    conn = op.get_bind()

    # --- Step 1: Platform tables ---
    op.create_table(
        "market_policies",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("market_id", sa.String(10), nullable=False),
        sa.Column("locale", sa.String(10), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("market_id", name="uq_mp_market_id"),
    )
    # ... market_policy_versions, market_policy_version_events ...

    # --- Step 2: Truth tables ---
    # ... truth_packages through fact_market_policy_dependencies ...

    # --- Step 3: Existing table evolution ---
    # Preflight: assert no orphaned truth_package_id values
    if _is_pg(conn):
        result = conn.execute(text(
            "SELECT id FROM asset_versions "
            "WHERE truth_package_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM truth_packages tp "
            "  WHERE tp.workspace_id = asset_versions.workspace_id "
            "  AND tp.id = asset_versions.truth_package_id) "
            "LIMIT 5"
        ))
        orphans = result.fetchall()
        if orphans:
            raise RuntimeError(
                f"Preflight failed: orphaned truth_package_id on asset_versions: "
                f"{[r[0] for r in orphans]}"
            )

    # Widen step_kind from String(20) to String(40)
    with op.batch_alter_table("provider_call_attempts") as batch:
        batch.alter_column("step_kind", type_=sa.String(40))
        batch.alter_column("spend_reservation_id", nullable=True)
        batch.add_column(sa.Column(
            "chargeability", sa.String(24), nullable=True,
        ))
        batch.add_column(sa.Column(
            "truth_package_id", sa.String(64), nullable=True,
        ))

    # Backfill chargeability
    conn.execute(text(
        "UPDATE provider_call_attempts SET chargeability = CASE "
        "WHEN execution_mode = 'byok' THEN 'charged' "
        "WHEN spend_reservation_id IS NOT NULL THEN 'legacy_reserved' "
        "WHEN execution_mode = 'mock' THEN 'free_mock' "
        "WHEN execution_mode = 'sandbox' THEN 'free_sandbox' "
        "ELSE 'charged' END"
    ))

    with op.batch_alter_table("provider_call_attempts") as batch:
        batch.alter_column("chargeability", nullable=False)

    # ... Steps 4-7: remaining tables in order ...

    # --- Step 8: PostgreSQL triggers, RLS, grants ---
    if _is_pg(conn):
        # Immutable triggers for all immutable tables
        # Forward-only state transition triggers
        # RLS + FORCE RLS policies
        # Grants
        pass


def downgrade() -> None:
    conn = op.get_bind()
    # Drop tables in reverse dependency order
    # Reverse existing table evolution
    pass
```

The migration file follows the exact DDL order from spec §1.11. Each `op.create_table()` call includes all columns, constraints, and foreign keys from the spec. The file will be large (~2000+ lines) but is a single atomic revision.

- [ ] **Step 3: Implement full upgrade() with all 32 tables**

Write the complete `upgrade()` function with all tables in the exact DDL order specified in §1.11. Include every column, every UNIQUE, every ForeignKeyConstraint, every CheckConstraint. For PostgreSQL-only features (triggers, RLS, FORCE RLS, grants), use `_is_pg(conn)` guards.

- [ ] **Step 4: Implement downgrade()**

Drop tables in reverse dependency order. Reverse all `batch_alter_table` changes on existing tables.

- [ ] **Step 5: Run migration test**

Run: `python -m pytest tests/test_localization_migration.py -v`

- [ ] **Step 6: Seed nine-market catalog in migration**

Add the nine market policies and their initial effective versions as data inserts in the migration, gated by "only if not already present."

- [ ] **Step 7: Commit**

```bash
git add migrations/versions/u1a2b3c4d5e6_localization_truth_bounded_qc.py tests/test_localization_migration.py
git commit -m "feat(localization): add Alembic migration for 32 tables + 2 evolutions (#24)"
```

**Acceptance tests:** §3I (full migration evidence — upgrade, downgrade, re-upgrade, metadata assertions, existing row survival, chargeability backfill, preflight).

---

### Task 8: FactSourceAdapter Protocol and Adapters

**Files:**
- Create: `app/localization/fact_sources.py`
- Create: `tests/contracts/test_fact_source_adapter_contract.py`
- Create: `tests/test_localization_evidence_tier.py`

**Interfaces:**
- Consumes: none (pure value objects and protocol)
- Produces: `FactResolutionRequest`, `RawSourceFact`, `FactSourceFailure`, `FactSourceResult`, `FactValue` union, `MoneyFactValue`, `AvailabilityFactValue`, `DiscountFactValue`, `ShippingFactValue`, `DeliveryFactValue`, `ProductClaimFactValue`, `FactSourceAdapter` protocol, `ManualFactSourceAdapter`, `ControlledMockFactSourceAdapter`

- [ ] **Step 1: Write shared adapter contract test**

```python
# tests/contracts/test_fact_source_adapter_contract.py
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from app.localization.fact_sources import (
    FactResolutionRequest, FactSourceAdapter, FactSourceResult,
    RawSourceFact, FactSourceFailure, MoneyFactValue, AvailabilityFactValue,
    ManualFactSourceAdapter, ControlledMockFactSourceAdapter,
    OfferContext, MarketContext, SourceMappingContext, DataSourceConfig,
    ManualFactSet,
)


class _AdapterContractMixin:
    """Shared contract: both Manual and Controlled Mock must pass."""
    adapter: FactSourceAdapter

    def _make_request(self, **overrides) -> FactResolutionRequest:
        defaults = dict(
            requested_fact_types=("price", "availability"),
            offer=OfferContext(offer_version_id="ov_1", product_version_id="pv_1"),
            market=MarketContext(market="de-DE", locale="de-DE", country="DE", currency="EUR"),
            source_mapping=SourceMappingContext(mapping_version_id="msm_1"),
            data_source=DataSourceConfig(data_source_version_id="dsv_1"),
            manual_fact_set=None,
            truth_data_mode="real",
            mock_scenario=None,
        )
        defaults.update(overrides)
        return FactResolutionRequest(**defaults)

    def test_returns_fact_source_result(self):
        result = self.adapter.resolve(self._make_request())
        self.assertIsInstance(result, FactSourceResult)

    def test_facts_are_raw_source_facts(self):
        result = self.adapter.resolve(self._make_request())
        for fact in result.facts:
            self.assertIsInstance(fact, RawSourceFact)

    def test_money_is_decimal_not_float(self):
        result = self.adapter.resolve(self._make_request())
        for fact in result.facts:
            if isinstance(fact.value, MoneyFactValue):
                self.assertIsInstance(fact.value.amount, Decimal)

    def test_availability_false_is_successful_fact(self):
        result = self.adapter.resolve(self._make_request(
            requested_fact_types=("availability",),
        ))
        avail_facts = [f for f in result.facts
                       if isinstance(f.value, AvailabilityFactValue)]
        if avail_facts:
            self.assertIsInstance(avail_facts[0].value.available, bool)

    def test_scope_key_is_concrete(self):
        result = self.adapter.resolve(self._make_request())
        for fact in result.facts:
            self.assertGreater(len(fact.scope_key), 0)
            self.assertNotIn("generic", fact.scope_key.lower())

    def test_evidence_hash_nonempty(self):
        result = self.adapter.resolve(self._make_request())
        for fact in result.facts:
            self.assertGreater(len(fact.evidence_hash), 0)


class TestManualAdapterContract(_AdapterContractMixin, unittest.TestCase):
    def setUp(self):
        self.adapter = ManualFactSourceAdapter()

    def _make_request(self, **overrides):
        overrides.setdefault("manual_fact_set", ManualFactSet(
            version_id="mfs_1",
            facts={"price": {"amount": "29.99", "currency": "EUR"}},
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        ))
        overrides.setdefault("truth_data_mode", "real")
        return super()._make_request(**overrides)


class TestControlledMockAdapterContract(_AdapterContractMixin, unittest.TestCase):
    def setUp(self):
        self.adapter = ControlledMockFactSourceAdapter()

    def _make_request(self, **overrides):
        overrides.setdefault("truth_data_mode", "mock")
        overrides.setdefault("mock_scenario", "standard_9_market")
        return super()._make_request(**overrides)

    def test_deterministic_for_same_input(self):
        req = self._make_request()
        r1 = self.adapter.resolve(req)
        r2 = self.adapter.resolve(req)
        self.assertEqual(
            [f.evidence_hash for f in r1.facts],
            [f.evidence_hash for f in r2.facts],
        )

    def test_different_scenario_different_output(self):
        r1 = self.adapter.resolve(self._make_request(mock_scenario="standard_9_market"))
        r2 = self.adapter.resolve(self._make_request(mock_scenario="unavailable"))
        self.assertNotEqual(
            [f.evidence_hash for f in r1.facts],
            [f.evidence_hash for f in r2.facts],
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/contracts/test_fact_source_adapter_contract.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement value objects and protocol**

Create `app/localization/fact_sources.py` with the exact types from spec §2B:

```python
# app/localization/fact_sources.py
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping, Protocol


@dataclass(frozen=True)
class OfferContext:
    offer_version_id: str
    product_version_id: str


@dataclass(frozen=True)
class MarketContext:
    market: str
    locale: str
    country: str
    currency: str


@dataclass(frozen=True)
class SourceMappingContext:
    mapping_version_id: str


@dataclass(frozen=True)
class DataSourceConfig:
    data_source_version_id: str


@dataclass(frozen=True)
class ManualFactSet:
    version_id: str
    facts: dict
    valid_from: datetime
    valid_to: datetime


@dataclass(frozen=True)
class MoneyFactValue:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class AvailabilityFactValue:
    available: bool


@dataclass(frozen=True)
class DiscountFactValue:
    discount_type: str
    amount: Decimal
    currency: str
    conditions: str


@dataclass(frozen=True)
class ShippingFactValue:
    method: str
    cost: Decimal
    currency: str
    delivery_days_min: int
    delivery_days_max: int


@dataclass(frozen=True)
class DeliveryFactValue:
    delivery_days_min: int
    delivery_days_max: int
    region: str


@dataclass(frozen=True)
class ProductClaimFactValue:
    claim_type: str
    claim_text: str
    evidence_ref: str


FactValue = (
    MoneyFactValue
    | AvailabilityFactValue
    | DiscountFactValue
    | ShippingFactValue
    | DeliveryFactValue
    | ProductClaimFactValue
)


@dataclass(frozen=True)
class FactResolutionRequest:
    requested_fact_types: tuple[str, ...]
    offer: OfferContext
    market: MarketContext
    source_mapping: SourceMappingContext
    data_source: DataSourceConfig
    manual_fact_set: ManualFactSet | None
    truth_data_mode: Literal["real", "mock"]
    mock_scenario: str | None


@dataclass(frozen=True)
class RawSourceFact:
    fact_type: str
    scope_key: str
    value: FactValue
    evidence_hash: str
    external_object_ref: str
    requested_context: Mapping[str, object]
    observed_at: datetime
    source_valid_until: datetime | None
    mock_scenario: str | None
    mock_version: str | None


@dataclass(frozen=True)
class FactSourceFailure:
    fact_type: str
    scope_key: str
    code: str
    message: str
    is_transient: bool


@dataclass(frozen=True)
class FactSourceResult:
    facts: tuple[RawSourceFact, ...]
    failures: tuple[FactSourceFailure, ...]


class FactSourceAdapter(Protocol):
    adapter_kind: str

    def resolve(self, request: FactResolutionRequest) -> FactSourceResult: ...
```

- [ ] **Step 4: Implement ManualFactSourceAdapter**

```python
class ManualFactSourceAdapter:
    adapter_kind = "manual"

    def resolve(self, request: FactResolutionRequest) -> FactSourceResult:
        if request.manual_fact_set is None:
            return FactSourceResult(facts=(), failures=tuple(
                FactSourceFailure(ft, f"{request.offer.offer_version_id}:{ft}",
                                  "no_manual_fact_set", "Manual fact set required", False)
                for ft in request.requested_fact_types
            ))
        # ... resolve facts from manual_fact_set.facts dict ...
        # Validate valid_from/valid_to interval
        # Return MoneyFactValue with Decimal, AvailabilityFactValue, etc.
        # Compute evidence_hash as SHA-256 of canonical fact content
```

- [ ] **Step 5: Implement ControlledMockFactSourceAdapter**

```python
class ControlledMockFactSourceAdapter:
    adapter_kind = "controlled_mock"
    _VERSION = "mock-v1"

    def resolve(self, request: FactResolutionRequest) -> FactSourceResult:
        # Deterministic output based on scenario + market + offer + requested types
        # Same input always produces same evidence_hash
        # Different scenario/version produces different output
```

- [ ] **Step 6: Write evidence-tier derivation test**

```python
# tests/test_localization_evidence_tier.py
import unittest
from app.localization.evidence_tier import derive_evidence_tier, EvidenceTier

class TestEvidenceTierDerivation(unittest.TestCase):
    def test_all_production(self):
        tier = derive_evidence_tier(
            source_modes=["byok"], experiment_modes=["production"],
            truth_data_mode="real", provider_mode="byok",
        )
        self.assertEqual(tier, EvidenceTier.PRODUCTION)

    def test_one_sandbox_source(self):
        tier = derive_evidence_tier(
            source_modes=["byok", "sandbox"], experiment_modes=["production", "production"],
            truth_data_mode="real", provider_mode="byok",
        )
        self.assertEqual(tier, EvidenceTier.SANDBOX)

    def test_any_mock_is_mock(self):
        tier = derive_evidence_tier(
            source_modes=["byok"], experiment_modes=["mock"],
            truth_data_mode="real", provider_mode="byok",
        )
        self.assertEqual(tier, EvidenceTier.MOCK)

    def test_mock_truth_is_mock(self):
        tier = derive_evidence_tier(
            source_modes=["byok"], experiment_modes=["production"],
            truth_data_mode="mock", provider_mode="byok",
        )
        self.assertEqual(tier, EvidenceTier.MOCK)

    def test_sandbox_provider_is_sandbox(self):
        tier = derive_evidence_tier(
            source_modes=["byok"], experiment_modes=["production"],
            truth_data_mode="real", provider_mode="sandbox",
        )
        self.assertEqual(tier, EvidenceTier.SANDBOX)
```

- [ ] **Step 7: Implement evidence_tier module**

```python
# app/localization/evidence_tier.py
from __future__ import annotations
from enum import IntEnum


class EvidenceTier(IntEnum):
    MOCK = 0
    SANDBOX = 1
    PRODUCTION = 2


_SOURCE_MAP = {"byok": EvidenceTier.PRODUCTION, "sandbox": EvidenceTier.SANDBOX, "mock": EvidenceTier.MOCK}
_EXPERIMENT_MAP = {"production": EvidenceTier.PRODUCTION, "sandbox": EvidenceTier.SANDBOX, "mock": EvidenceTier.MOCK}
_TRUTH_MAP = {"real": EvidenceTier.PRODUCTION, "mock": EvidenceTier.MOCK}
_PROVIDER_MAP = {"byok": EvidenceTier.PRODUCTION, "sandbox": EvidenceTier.SANDBOX, "mock": EvidenceTier.MOCK}


def derive_evidence_tier(
    *,
    source_modes: list[str],
    experiment_modes: list[str],
    truth_data_mode: str,
    provider_mode: str,
) -> EvidenceTier:
    tiers = [_SOURCE_MAP[m] for m in source_modes]
    tiers += [_EXPERIMENT_MAP[m] for m in experiment_modes]
    tiers.append(_TRUTH_MAP[truth_data_mode])
    tiers.append(_PROVIDER_MAP[provider_mode])
    return min(tiers)
```

- [ ] **Step 8: Run all tests and commit**

Run: `python -m pytest tests/contracts/test_fact_source_adapter_contract.py tests/test_localization_evidence_tier.py -v`

```bash
git add app/localization/fact_sources.py app/localization/evidence_tier.py tests/contracts/test_fact_source_adapter_contract.py tests/test_localization_evidence_tier.py
git commit -m "feat(localization): add FactSourceAdapter protocol, Manual/Mock adapters, evidence tier (#24)"
```

**Acceptance tests:** §3C (evidence-tier derivation), §3D (fact-source adapter contract), §16.4 (resolution/fallback/provenance).

---

### Task 9: Truth Resolution Module

**Files:**
- Create: `app/localization/truth.py`
- Create: `tests/test_localization_truth.py`
- Create: `tests/test_localization_truth_attempts.py`

**Interfaces:**
- Consumes: `FactSourceAdapter`, `FactSourceResult`, `TruthPackageRecord`, `TruthPackageMarketRecord`, `TruthSourceAttemptRecord`, `TruthFactRecord`, `TruthDerivedFactRecord`, `TruthDerivedFactInputRecord`, `FactLibraryDependencyRecord`, `FactMarketPolicyDependencyRecord`, `evidence_tier.derive_evidence_tier`
- Produces: `resolve_or_reuse_truth_package(session, ctx, command) -> TruthResolutionResult`, `assess_truth_freshness(session, ctx, truth_package_id, *, at) -> FreshnessReport`

- [ ] **Step 1: Write truth resolution test**

```python
# tests/test_localization_truth.py
import unittest
from app.localization.truth import resolve_or_reuse_truth_package, TruthResolutionResult

class TestTruthResolution(unittest.TestCase):
    def test_resolve_creates_package_with_full_status(self):
        # Use Manual adapter with complete manual facts
        # Assert package status is 'full'
        # Assert all nine markets have readiness entries
        pass

    def test_resolve_reuses_matching_canonical_hash(self):
        # Resolve twice with same inputs
        # Assert same truth_package_id returned
        pass

    def test_degraded_when_permitted_fact_missing(self):
        # Missing non-price fact with on_failure=skip
        # Assert package status is 'degraded'
        pass

    def test_failed_when_no_executable_market(self):
        # All markets unavailable
        # Assert package status is 'failed'
        pass

    def test_mixed_real_mock_rejected(self):
        # Real DataSource + Mock DataSource in same resolution
        # Assert rejection, not downgrade
        pass
```

- [ ] **Step 2: Write truth source attempt persistence test**

```python
# tests/test_localization_truth_attempts.py
import unittest

class TestTruthSourceAttempts(unittest.TestCase):
    def test_accepted_attempt_has_evidence_hash(self):
        pass
    def test_failed_attempt_has_failure_code(self):
        pass
    def test_skipped_attempt_has_skip_code(self):
        pass
    def test_fallback_creates_multiple_attempts(self):
        pass
    def test_fact_references_accepted_attempt(self):
        pass
    def test_immutable_trigger_rejects_update(self):
        pass
```

- [ ] **Step 3: Implement truth.py**

```python
# app/localization/truth.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from sqlalchemy.orm import Session
from ..platform.tenancy import TenantContext

@dataclass(frozen=True)
class TruthResolutionResult:
    truth_package_id: str
    status: str  # 'full' | 'degraded' | 'failed'
    reused: bool
    market_readiness: dict[str, str]

@dataclass(frozen=True)
class FreshnessReport:
    is_current: bool
    reasons: tuple[str, ...]

def resolve_or_reuse_truth_package(
    session: Session,
    ctx: TenantContext,
    command: TruthResolutionCommand,
) -> TruthResolutionResult:
    # 1. Load effective versions
    # 2. Validate workspace/offer/market
    # 3. For each market: resolve facts via adapter
    # 4. Persist source attempts (accepted, failed, skipped)
    # 5. Freeze raw facts, derive formatted/stacked/max
    # 6. Derive readiness per market, package status
    # 7. Compute canonical hash
    # 8. Check for reuse (same hash + still fresh)
    # 9. Write atomically
    ...

def assess_truth_freshness(
    session: Session,
    ctx: TenantContext,
    truth_package_id: str,
    *,
    at: datetime,
) -> FreshnessReport:
    # Check valid_until > at
    # Check library version lifecycle
    # Check market policy version lifecycle
    ...
```

- [ ] **Step 4: Run tests and commit**

```bash
git add app/localization/truth.py tests/test_localization_truth.py tests/test_localization_truth_attempts.py
git commit -m "feat(localization): add truth resolution module (#24)"
```

**Acceptance tests:** §3D (fact-source adapter + attempt persistence), §16.4-§16.5 (resolution/fallback, no inferred facts).

---

### Task 10: Generic Provider Dispatch Module

**Files:**
- Create: `app/providers/dispatch.py`
- Create: `app/providers/attempts.py`
- Modify: `app/providers/adapter.py` (generalize from image-specific)
- Create: `tests/contracts/test_external_call_adapter_contract.py`
- Create: `tests/test_provider_dispatch.py`

**Interfaces:**
- Consumes: `ProviderCallAttemptRecord`, `OperationRecord`, `TenantContext`
- Produces: `ExternalCallAdapter[RequestT, ResultT]` protocol, `ProviderCapabilities`, `ExternalCallEvidenceRecorder[ResultT]`, `ExternalCallCoordinator`, `CoordinatedCallResult[ResultT]`

- [ ] **Step 1: Write adapter contract test**

```python
# tests/contracts/test_external_call_adapter_contract.py
import unittest

class TestExternalCallAdapterContract(unittest.TestCase):
    def test_adapter_has_provider_and_capabilities(self):
        pass
    def test_execute_returns_provider_result(self):
        pass
    def test_credential_is_memoryview(self):
        pass
    def test_idempotency_key_propagated(self):
        pass
```

- [ ] **Step 2: Write coordinator crash-window tests**

```python
# tests/test_provider_dispatch.py
class TestExternalCallCoordinator(unittest.TestCase):
    def test_window_1_job_claimed_before_prepare(self):
        pass  # Reclaim and prepare idempotently
    def test_window_2_prepared_before_network(self):
        pass  # Redispatch same authorized call
    def test_window_3_response_before_completion(self):
        pass  # Uncertain; reconcile with provider evidence
    def test_window_4_completion_before_fold(self):
        pass  # Replay fold without provider call
    def test_window_5_request_sent_no_response(self):
        pass  # Capability-aware reconciliation
```

- [ ] **Step 3: Implement generic adapter protocol and coordinator**

Implement `ExternalCallAdapter`, `ExternalCallCoordinator` with three-phase dispatch per spec §2D. The coordinator is domain-agnostic; `step_kind` is metadata, not a dispatch switch.

- [ ] **Step 4: Ensure generation compatibility**

Re-export existing generation imports. Existing `app/generation/worker.py` must continue to work without changes.

- [ ] **Step 5: Run tests and commit**

```bash
git add app/providers/ tests/contracts/test_external_call_adapter_contract.py tests/test_provider_dispatch.py
git commit -m "feat(providers): generalize external-call dispatch module (#24)"
```

**Acceptance tests:** §3G (five crash windows, coordinator never imports localization models).

---

### Task 11: Localization Eligibility Module

**Files:**
- Create: `app/localization/eligibility.py`
- Create: `tests/test_localization_eligibility.py`

**Interfaces:**
- Consumes: `ExperimentDecisionRecord`, `ExperimentEligibilityEventRecord`, `AssetVersionRecord`, `QualityReviewRecord`, `TenantContext`
- Produces: `list_eligible_winners(session, ctx, *, requested_evidence_tier) -> list[EligibleWinner]`, `validate_source_for_batch(session, ctx, asset_version_id, decision_id, *, requested_evidence_tier) -> ValidationReport`

- [ ] **Step 1: Write eligibility tests per §16.1**

```python
# tests/test_localization_eligibility.py
import unittest

class TestSourceWinnerEligibility(unittest.TestCase):
    def test_reject_no_decision(self):
        pass
    def test_reject_inconclusive_decision(self):
        pass
    def test_reject_non_winner_decision(self):
        pass
    def test_reject_missing_confirmation(self):
        pass
    def test_reject_no_approved_review(self):
        pass
    def test_reject_blocking_qc_failure(self):
        pass
    def test_reject_withdrawn_product(self):
        pass
    def test_reject_incompatible_evidence_tier(self):
        pass
    def test_accept_valid_winner(self):
        pass
    def test_stale_discovery_projection_still_rejects(self):
        pass
```

- [ ] **Step 2: Implement eligibility.py**

```python
# app/localization/eligibility.py
from __future__ import annotations
from dataclasses import dataclass
from sqlalchemy.orm import Session
from ..platform.tenancy import TenantContext

@dataclass(frozen=True)
class EligibleWinner:
    asset_version_id: str
    decision_id: str
    experiment_id: str
    experiment_version_id: str
    experiment_version_candidate_id: str
    source_generation_mode: str
    experiment_evidence_mode: str

@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    findings: tuple[str, ...]

def list_eligible_winners(
    session: Session, ctx: TenantContext,
    *, requested_evidence_tier: str | None = None,
) -> list[EligibleWinner]:
    # Uses ContextualVerdictProjectionRecord for discovery only
    # Never filters by DataSource mode
    ...

def validate_source_for_batch(
    session: Session, ctx: TenantContext,
    asset_version_id: str, decision_id: str,
    *, requested_evidence_tier: str,
) -> ValidationReport:
    # Reads authoritative ExperimentDecisionRecord, confirmation, candidate
    # Checks review state, QC, product/offer context, source market
    ...
```

- [ ] **Step 3: Run tests and commit**

```bash
git add app/localization/eligibility.py tests/test_localization_eligibility.py
git commit -m "feat(localization): add eligibility module (#24)"
```

**Acceptance tests:** §16.1 (source Winner eligibility — all rejection cases).

---

### Task 12: Localization Claims Module

**Files:**
- Create: `app/localization/claims.py`
- Create: `tests/test_localization_claims.py`

**Interfaces:**
- Consumes: `SourceClaimInventoryRecord`, `SourceClaimConfirmationRecord`, `LocalizationClaimDecisionSetRecord`, `LocalizationClaimDecisionRecord`, `TruthFactRecord`, eligibility module
- Produces: `plan_inventories()`, `create_inventory_from_analysis()`, `submit_claim_decisions()`, `get_or_create_decision_set()`

- [ ] **Step 1: Write claims tests per §16.7**

```python
# tests/test_localization_claims.py
class TestClaimsModule(unittest.TestCase):
    def test_replace_requires_same_scope_fact(self):
        pass
    def test_remove_freezes_removal_strategy(self):
        pass
    def test_preserve_requires_market_neutral_policy(self):
        pass
    def test_incomplete_decision_set_rejected(self):
        pass
    def test_canonical_hash_mismatch_rejected(self):
        pass
    def test_high_risk_claim_blocks_copy(self):
        pass
    def test_reuse_decision_set_when_hash_matches(self):
        pass
```

- [ ] **Step 2: Implement claims.py**

- [ ] **Step 3: Run tests and commit**

```bash
git add app/localization/claims.py tests/test_localization_claims.py
git commit -m "feat(localization): add claims module (#24)"
```

**Acceptance tests:** §16.7 (claims, locked copy, QC).

---

### Task 13: Localization Copy Module

**Files:**
- Create: `app/localization/copy.py`
- Create: `tests/test_localization_copy.py`

**Interfaces:**
- Consumes: `LocalizedCopyVersionRecord`, `LocalizedCopyApprovalRecord`, claims module, truth module
- Produces: `plan_copy_versions()`, `create_copy_from_translation()`, `create_source_locale_projection()`, `approve_copy_version()`

- [ ] **Step 1: Write copy tests**

```python
# tests/test_localization_copy.py
class TestCopyModule(unittest.TestCase):
    def test_source_locale_skips_translation_not_policy(self):
        pass
    def test_translation_receives_locked_placeholders(self):
        pass
    def test_reuse_requires_matching_fingerprint(self):
        pass
    def test_stale_truth_prevents_reuse(self):
        pass
    def test_approve_creates_immutable_record(self):
        pass
    def test_copy_series_includes_all_provenance(self):
        pass
```

- [ ] **Step 2: Implement copy.py**

```python
# app/localization/copy.py
# plan_copy_versions: returns reused, projected, and missing-translation work
# create_source_locale_projection: deterministic, no translation call
# create_copy_from_translation: external call result → immutable copy version
# approve_copy_version: append-only approval
```

- [ ] **Step 3: Run tests and commit**

```bash
git add app/localization/copy.py tests/test_localization_copy.py
git commit -m "feat(localization): add copy module (#24)"
```

**Acceptance tests:** §16.7 (copy/translation), nine-market matrix including source-locale paths.

---

### Task 14: Localization Batch Module

**Files:**
- Create: `app/localization/batch.py`
- Create: `tests/test_localization_batch.py`

**Interfaces:**
- Consumes: eligibility, claims, copy, truth, evidence_tier modules; all batch/item/attempt ORM records
- Produces: `create_draft()`, `start_preparation()`, `submit_claim_decisions()`, `approve_copy()`, `confirm()`, `retry_item()`, `resume_item()`, `authorize_isolated_qc()`, `adopt_isolated_output()`, `cancel()`, `derive_batch_outcome()`

- [ ] **Step 1: Write batch command sequence tests per §3F**

```python
# tests/test_localization_batch.py
class TestBatchCommandSequence(unittest.TestCase):
    def test_analysis_incomplete_blocks_translation(self):
        pass
    def test_unresolved_high_risk_blocks_translation(self):
        pass
    def test_reused_work_creates_no_operation(self):
        pass
    def test_entitlement_failure_rolls_back_fully(self):
        pass
    def test_frozen_field_cannot_be_rewritten(self):
        pass
    def test_same_semantic_command_replays(self):
        pass
    def test_confirm_freezes_batch_creates_items_attempt1(self):
        pass
    def test_retry_item_creates_attempt2(self):
        pass
    def test_attempt4_rejected(self):
        pass
    def test_derive_outcome_succeeded(self):
        pass
    def test_derive_outcome_partially_succeeded(self):
        pass
    def test_derive_outcome_failed(self):
        pass
```

- [ ] **Step 2: Implement batch.py**

The batch module owns the complete command sequence from spec §2E. Each command follows the pattern: validate → admit (PaidOperationAdmission for external calls) → CAS transition → persist → return.

- [ ] **Step 3: Run tests and commit**

```bash
git add app/localization/batch.py tests/test_localization_batch.py
git commit -m "feat(localization): add batch module (#24)"
```

**Acceptance tests:** §3F (staged preparation/freeze), §16.2 (batch source consistency), §16.6 (truth/batch outcomes), §16.8 (bounded retries/idempotency/cost), §16.9 (semantic reuse).

---

### Task 15: Localization QC Module

**Files:**
- Create: `app/localization/qc.py`
- Create: `tests/test_localization_qc.py`

**Interfaces:**
- Consumes: `LocalizationQcRecordRecord`, `LocalizationAttemptOutputRecord`, truth facts, copy versions
- Produces: `run_deterministic_qc()`, `derive_qc_eligibility()`, `get_frozen_qc_plan()`

- [ ] **Step 1: Write QC tests per §16.7**

```python
# tests/test_localization_qc.py
class TestDeterministicQC(unittest.TestCase):
    def test_locked_fact_mismatch_is_blocking(self):
        pass
    def test_number_match_wrong_format(self):
        pass
    def test_currency_match_wrong_symbol(self):
        pass
    def test_claim_decision_replace_verified(self):
        pass
    def test_placeholder_count_mismatch(self):
        pass
    def test_new_commercial_claim_is_blocking(self):
        pass
    def test_forbidden_term_detected(self):
        pass
    def test_warning_severity_does_not_block(self):
        pass
    def test_idempotent_fold_per_check_slot(self):
        pass
```

- [ ] **Step 2: Implement qc.py**

- [ ] **Step 3: Run tests and commit**

```bash
git add app/localization/qc.py tests/test_localization_qc.py
git commit -m "feat(localization): add QC module (#24)"
```

**Acceptance tests:** §16.7 (all QC checks), §3H (no QC waiver, no qc_passed field).

---

### Task 16: Localization Worker Module

**Files:**
- Create: `app/localization/worker.py`
- Create: `tests/test_localization_worker.py`

**Interfaces:**
- Consumes: `ExternalCallCoordinator`, batch module, QC module, eligibility module, all ORM records
- Produces: Job handlers for `localization.analyze_source`, `localization.translate_copy`, `localization.edit_attempt`, `localization.run_deterministic_qc`, `localization.ocr_check`, `localization.visual_qc`, `localization.finalize_item`, `localization.fold_batch`

- [ ] **Step 1: Write worker tests per §2G**

```python
# tests/test_localization_worker.py
class TestEditAttemptHandler(unittest.TestCase):
    def test_accepts_only_authorized_queued_attempt(self):
        pass
    def test_rechecks_eligibility_before_call(self):
        pass
    def test_persists_output_before_qc(self):
        pass
    def test_rechecks_eligibility_after_response(self):
        pass
    def test_deterministic_qc_failure_cancels_external_qc(self):
        pass
    def test_all_blocking_pass_enqueues_finalize(self):
        pass
    def test_attempt3_failure_is_terminal(self):
        pass

class TestFinalizeItemHandler(unittest.TestCase):
    def test_rechecks_all_before_publish(self):
        pass
    def test_creates_child_asset_version_lineage_binding(self):
        pass
    def test_child_has_no_quality_review(self):
        pass
```

- [ ] **Step 2: Implement worker.py**

Follow the existing `app/generation/worker.py` pattern: synchronous handlers, state guards, CAS transitions, three-phase dispatch through the coordinator.

- [ ] **Step 3: Run tests and commit**

```bash
git add app/localization/worker.py tests/test_localization_worker.py
git commit -m "feat(localization): add worker module (#24)"
```

**Acceptance tests:** §3G (provider dispatch/crash/reconciliation), §16.8 (bounded retries), §16.10 (cancellation/runtime invalidation).

---

### Task 17: Localization Binding Module

**Files:**
- Create: `app/localization/binding.py`
- Create: `tests/test_localization_binding.py`

**Interfaces:**
- Consumes: `MarketAssetBindingRecord`, `MarketAssetBindingEventRecord`, `LocalizedAssetLineageRecord`, `FactLibraryDependencyRecord`, `FactMarketPolicyDependencyRecord`, truth freshness, eligibility
- Produces: `publish_binding()`, `assess_binding_eligibility()`, `apply_dependency_change()`

- [ ] **Step 1: Write binding and stale propagation tests per §16.11-§16.12**

```python
# tests/test_localization_binding.py
class TestBindingPublication(unittest.TestCase):
    def test_publish_supersedes_active_binding(self):
        pass
    def test_lineage_is_immutable(self):
        pass
    def test_child_starts_pending_review(self):
        pass
    def test_source_reuse_references_existing_version(self):
        pass

class TestStalePropagation(unittest.TestCase):
    def test_superseded_data_source_stales_binding(self):
        pass
    def test_superseded_market_policy_stales_binding(self):
        pass
    def test_historical_truth_unchanged(self):
        pass
    def test_event_delivery_idempotent(self):
        pass
    def test_downstream_experiment_invalidation_precise(self):
        pass
    def test_credential_rotation_does_not_stale(self):
        pass
```

- [ ] **Step 2: Implement binding.py**

- [ ] **Step 3: Run tests and commit**

```bash
git add app/localization/binding.py tests/test_localization_binding.py
git commit -m "feat(localization): add binding module (#24)"
```

**Acceptance tests:** §16.11 (lineage/review), §16.12 (stale propagation).

---

### Task 18: FastAPI Routes and Permissions

**Files:**
- Create: `app/localization/routes.py`
- Create: `tests/test_localization_routes.py`
- Create: `tests/test_localization_permissions.py`

**Interfaces:**
- Consumes: batch module, eligibility module, TenantContext from BFF claims
- Produces: FastAPI router with endpoints for all localization commands and queries

- [ ] **Step 1: Write route and permission tests per §16.13**

```python
# tests/test_localization_routes.py
class TestLocalizationRoutes(unittest.TestCase):
    def test_create_draft(self):
        pass
    def test_start_preparation(self):
        pass
    def test_confirm(self):
        pass
    def test_cancel(self):
        pass

# tests/test_localization_permissions.py
class TestLocalizationPermissions(unittest.TestCase):
    def test_member_cannot_publish_data_source(self):
        pass
    def test_owner_publish_requires_mfa(self):
        pass
    def test_member_can_start_preparation(self):
        pass
    def test_cross_workspace_rejected(self):
        pass
    def test_worker_scope_from_job_context(self):
        pass
```

- [ ] **Step 2: Implement routes**

```python
# app/localization/routes.py
from fastapi import APIRouter

router = APIRouter(prefix="/localization", tags=["localization"])

@router.post("/batches")
async def create_draft(...): ...

@router.post("/batches/{batch_id}/start-preparation")
async def start_preparation(...): ...

@router.post("/batches/{batch_id}/confirm")
async def confirm(...): ...

@router.post("/batches/{batch_id}/cancel")
async def cancel_batch(...): ...

@router.post("/batches/{batch_id}/items/{item_id}/retry")
async def retry_item(...): ...
```

- [ ] **Step 3: Run tests and commit**

```bash
git add app/localization/routes.py tests/test_localization_routes.py tests/test_localization_permissions.py
git commit -m "feat(localization): add FastAPI routes and permissions (#24)"
```

**Acceptance tests:** §16.13 (permissions, tenancy, redaction).

---

### Task 19: PostgreSQL Evidence Tests

**Files:**
- Create: `tests/test_localization_rls.py`
- Create: `tests/test_localization_triggers.py`
- Create: `tests/test_localization_cas.py`

**Interfaces:**
- Consumes: all ORM records, migration
- Produces: PostgreSQL-specific evidence for RLS, triggers, CAS, composite FKs

- [ ] **Step 1: Write RLS tests**

```python
# tests/test_localization_rls.py
class TestLocalizationRLS(unittest.TestCase):
    def test_tenant_a_cannot_read_tenant_b_truth(self):
        pass
    def test_tenant_a_cannot_read_tenant_b_batch(self):
        pass
    def test_force_rls_applies_to_superuser(self):
        pass
```

- [ ] **Step 2: Write trigger tests**

```python
# tests/test_localization_triggers.py
class TestImmutableTriggers(unittest.TestCase):
    def test_truth_fact_update_rejected(self):
        pass
    def test_lineage_delete_rejected(self):
        pass
    def test_source_attempt_update_rejected(self):
        pass
    def test_market_policy_version_forward_only(self):
        pass

class TestCompositeFK(unittest.TestCase):
    def test_cross_batch_item_rejected(self):
        pass
    def test_cross_source_attempt_rejected(self):
        pass
    def test_cross_workspace_rejected(self):
        pass
    def test_cross_market_fact_replacement_rejected(self):
        pass
```

- [ ] **Step 3: Write CAS tests**

```python
# tests/test_localization_cas.py
class TestCASConflicts(unittest.TestCase):
    def test_concurrent_batch_update_loses(self):
        pass
    def test_concurrent_item_transition_loses(self):
        pass
    def test_concurrent_binding_update_loses(self):
        pass
```

- [ ] **Step 4: Run and commit**

```bash
git add tests/test_localization_rls.py tests/test_localization_triggers.py tests/test_localization_cas.py
git commit -m "test(localization): add PostgreSQL evidence tests (#24)"
```

**Acceptance tests:** §3I (all PostgreSQL-specific evidence).

---

### Task 20: Playwright Browser Tests

**Files:**
- Create: `web/e2e/localization/golden-path.spec.ts`
- Create: `web/e2e/localization/recovery.spec.ts`

**Interfaces:**
- Consumes: FastAPI routes, BFF bridge, Controlled Mock adapters
- Produces: end-to-end browser test evidence

- [ ] **Step 1: Write golden path test per §16.14**

```typescript
// web/e2e/localization/golden-path.spec.ts
test('localization golden path with three markets', async ({ page }) => {
  // 1. Start from existing Approved, confirmed Winner
  // 2. Select source, Offer, three markets (en-GB, de-DE, ko-KR)
  // 3. Preview Truth with provenance
  // 4. Complete analysis and per-market Claim decisions
  // 5. Generate, validate, approve copy
  // 6. Confirm and freeze Batch
  // 7. Process: one success, one no-change/unavailable, one blocking-QC
  // 8. Retry blocked item and succeed
  // 9. Verify new localized child is pending review
  // 10. Approve and verify experiment entry
});
```

- [ ] **Step 2: Write recovery test**

```typescript
// web/e2e/localization/recovery.spec.ts
test('cancel during delayed edit, adopt after QC', async ({ page }) => {
  // Cancel during delayed Controlled Mock edit
  // Verify output stays isolated
  // Verify direct adoption blocked
  // Authorize isolated QC
  // Wait for all checks to pass
  // Adopt once
  // Verify AssetVersion starts unreviewed
});
```

- [ ] **Step 3: Run and commit**

```bash
git add web/e2e/localization/
git commit -m "test(localization): add Playwright golden-path and recovery tests (#24)"
```

**Acceptance tests:** §16.14 (browser golden path), §3H (isolated-output QC and adoption).

---

### Task 21: Final Verification and PR

**Files:**
- No new files

**Interfaces:**
- Consumes: all tasks above
- Produces: passing `make verify`, PR referencing Issue #24

- [ ] **Step 1: Run make verify**

```bash
make verify
```

- [ ] **Step 2: Verify completion gate per §3J**

Confirm:
- Every §16 case has an automated test
- Manual and Controlled Mock pass shared contract suite
- No paid path lacks Job/attempt/certainty/cost evidence
- All nine markets pass currency/context/freshness checks
- Python, PostgreSQL and Playwright suites pass
- Stable reason codes and safe next actions
- No test relies on live Shopify or unimplemented stubs

- [ ] **Step 3: Open PR**

```bash
gh pr create --title "feat(localization): truth, bounded QC, asset lineage (#24)" --body "$(cat <<'EOF'
## Summary
- 32 new tables + 2 existing table evolutions in one Alembic revision
- Nine-market catalog with full policy rules
- Truth resolution with Manual/ControlledMock adapters and fallback provenance
- Generic three-phase provider dispatch with five crash-window recovery
- Staged preparation: analysis → claim decisions → copy → confirm
- Bounded QC: deterministic + external, no waiver, three-attempt cap
- Atomic publication with immutable lineage and active binding
- Stale propagation through typed dependency index
- Evidence-tier derivation across four provenance dimensions

## Test plan
- [ ] `test_localization_platform.py` — nine-market seed
- [ ] `test_fact_source_adapter_contract.py` — Manual + ControlledMock
- [ ] `test_localization_truth.py` — resolution/reuse/freshness
- [ ] `test_localization_batch.py` — command sequence, freeze, outcome
- [ ] `test_localization_worker.py` — five crash windows, bounded retry
- [ ] `test_localization_qc.py` — all check kinds, no waiver
- [ ] `test_localization_binding.py` — publication, stale propagation
- [ ] `test_localization_rls.py` — tenant isolation
- [ ] `test_localization_triggers.py` — immutability, composite FKs
- [ ] `golden-path.spec.ts` — three-market browser test
- [ ] `recovery.spec.ts` — cancel/adopt browser test

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Verify PR created**

Run: `gh pr view --web`
