# Task 7 Report: Alembic Migration Revision

**Status:** Complete
**Revision:** `u1a2b3c4d5e6` (down_revision: `t1a2b3c4d5e6`)

## Files Changed

| File | Action |
|------|--------|
| `migrations/versions/u1a2b3c4d5e6_localization_truth_bounded_qc.py` | Created (~2700 lines) |
| `tests/test_localization_migration.py` | Created |
| `tests/test_postgres_rls.py` | Updated (RLS coverage tracking) |
| `tests/test_library_migration.py` | Updated (head revision assertions) |
| `tests/test_library_ui_gate0_migration.py` | Updated (head revision assertion) |

## Migration Contents

### New Tables (32)

**Step 1 -- Platform-scoped (no RLS):**
- `market_policies`, `market_policy_versions`, `market_policy_version_events`

**Step 2 -- Truth resolution:**
- `truth_packages`, `truth_package_markets`, `truth_source_attempts`, `truth_facts`, `truth_derived_facts`, `truth_derived_fact_inputs`, `fact_library_dependencies`, `fact_market_policy_dependencies`

**Step 4 -- Batch core:**
- `localization_batches`, `localization_batch_sources`, `localization_batch_commands` (deferred item FK), `localization_batch_market_targets`

**Step 5 -- Claims and copy:**
- `source_claim_inventories`, `source_claim_confirmations`, `localization_claim_decision_sets`, `localization_claim_decisions`, `localized_copy_versions`, `localized_copy_approvals`

**Step 6 -- Items, attempts, QC, provider calls:**
- `localization_items`, `localization_attempts`, `localization_attempt_outputs`, `localization_output_publications`, `localization_qc_records`, `localization_semantic_claims`, `localization_item_provider_calls`, `localization_preparation_provider_calls`

**Step 7 -- Bindings and lineage:**
- `market_asset_bindings`, `market_asset_binding_events`, `localized_asset_lineages`

### Existing Table Evolutions (2)

- **`asset_versions`**: Added `uq_av_workspace_truth_parent` unique constraint; PG-only `ck_av_localized_requires_truth_parent` CHECK
- **`provider_call_attempts`**: Widened `step_kind` to `String(40)`, made `spend_reservation_id` nullable, added `chargeability` (NOT NULL after backfill) and `truth_package_id` columns, added `uq_provider_call_attempt_alt_key` (both dialects), PG-only CHECKs and FK

### PostgreSQL Enforcement (Step 8)

- Append-only triggers on 26 tables
- RLS (ENABLE + FORCE) on 29 workspace-scoped tables
- Grants: SELECT+INSERT for immutable, SELECT+INSERT+UPDATE for mutable, SELECT-only for platform tables
- Nine-market seed (idempotent): en-GB, en-AU, en-CA, de-DE, fr-FR, es-ES, it-IT, ja-JP, ko-KR

## Test Evidence

```
make verify: 1485 passed, 190 skipped, 0 failed
tests/test_localization_migration.py: 2 passed
tests/test_postgres_rls.py: 6 passed (coverage tests), 31 skipped (PG-only)
```

### RLS Coverage

- Added `localization_truth_bounded_qc` group (32 tables) to `CURRENT_AGGREGATE_TABLES`
- Imported `NEW_WORKSPACE_OWNED` (29 tables) + `PLATFORM_TABLES` (3 tables) into `all_protected_tables()`
- `test_every_current_table_is_classified_in_exactly_one_aggregate`: PASS
- `test_the_migration_covers_every_table_in_the_model`: PASS

## Design Decisions

1. **Alt-key UQ on both dialects**: The `uq_provider_call_attempt_alt_key` unique constraint is created via `batch_alter_table` (not PG-only raw SQL) because `localization_item_provider_calls` and `localization_preparation_provider_calls` reference it with composite FKs that must be valid on SQLite too (PRAGMA foreign_key_check).

2. **Deferred FK pattern**: `localization_batch_commands.item_id` FK to `localization_items` is created after the items table exists, PG-only via raw SQL (SQLite does not support ALTER TABLE ADD CONSTRAINT for FKs).

3. **Chargeability backfill**: Existing `provider_call_attempts` rows are backfilled based on `execution_mode` and `spend_reservation_id` before the column is set NOT NULL.

4. **Nine-market seed**: Uses raw SQL with idempotent INSERT (skips if `market_id` already exists). IDs are generated with UUID hex prefixes matching the domain convention.

## Concerns

- The migration is very large (~2700 lines). This is expected given 32 tables with extensive constraints and composite FKs.
- Platform-scoped tables (market_policies*) have no RLS by design -- they are not workspace-scoped. Access is controlled by grants only.
- The nine-market seed generates random UUIDs at migration time. For reproducible environments, the seed should be run once and the IDs recorded.

## Post-Review Fix (C1 + C2)

Two critical issues identified by code review and fixed:

**C1: `ck_asset_version_origin_kind` CHECK not updated to include 'localized'**
- The existing CHECK from `n1_generation_assets_expand.py` constrained `origin_kind IN ('generation', 'upload', 'reuse', 'legacy')`.
- Fix: In `_step3_existing_table_evolution`, on PG: `DROP CONSTRAINT ck_asset_version_origin_kind` then re-add with `'localized'` included.
- Downgrade reverses: drops widened CHECK, re-adds original without `'localized'`.

**C2: Missing FK from `asset_versions` to `truth_packages`**
- ORM model declares `fk_asset_versions_workspace_truth_pkg_truth_packages` as composite FK `(workspace_id, truth_package_id)` -> `truth_packages(workspace_id, id)`.
- The migration creates `truth_packages` in step 2, so no prior migration could have this FK. The original comment was incorrect.
- Fix: Added PG-only `ALTER TABLE asset_versions ADD CONSTRAINT ... FOREIGN KEY` in step 3.
- Downgrade drops it before the `truth_packages` table is dropped.

**Test evidence after fix:** `make verify` -- 1485 passed, 190 skipped, 0 failures.
