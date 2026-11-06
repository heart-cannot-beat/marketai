# Issue #22 — Executable Implementation Plan

> **Source**: Complete design `078eb96b` (5,471 lines), approved 2026-09-03.
> **Order**: test-first per phase; migration → domain/service → API → worker/outbox → RLS/grants → acceptance.
> **Constraint**: No implementation may begin until `main` is synced and `make verify` passes on the branch.

---

## Phase 0: Sync and infrastructure

### 0.1 Sync latest `main` into `claude/issue-22-immutable-contextual-experiment-decisions`

```bash
git fetch origin main
git merge origin/main --no-edit
make verify
```

### 0.2 Consolidate canonical JSON into a shared utility

**Why**: Six independent `_canonical_json` / `canonical_hash` implementations exist across `app/experiments/judge.py:179`, `app/store.py:85`, `app/meta/service.py:64`, `app/library/contracts.py:215`, `app/generation/preview.py:174`, `app/generation/snapshot.py:58`. §5.3.1 requires a single `_canonical_json` / `_canonical_bytes` with float rejection, Decimal normalization, tz-aware datetime UTC normalization, and `sort_keys=True`.

**File**: `app/platform/canonical.py` (new)

| Function | Signature | §5 tests |
|---|---|---|
| `_reject_floats(obj)` | Raises `TypeError` on `float` | CE-06, CE-07 |
| `_canonical_decimal_str(d)` | `Decimal → str` no exponent, no trailing zeros, no negative zero | CE-02..CE-05, CD-P1..P4 |
| `_canonical_json(value)` | `Any → str` sorted keys, compact separators | CE-01..CE-16 |
| `_canonical_bytes(value)` | `Any → bytes` UTF-8 encode of `_canonical_json` | — |
| `_sha256(data: bytes)` | `bytes → str` hex digest | HD-01..HD-09 |
| `_normalize_legacy_numerics(obj)` | Recursive float→Decimal | NL-01..NL-03 |

**Test file**: `tests/test_canonical_json.py` (new) — CE-01..CE-16, CD-P1..P4, NL-01..NL-03, HD-01..HD-09.

After consolidation, update all six call sites to import from `app.platform.canonical`. Verify `make verify` passes.

### 0.3 Create pytest markers (if absent)

Add to `pyproject.toml` (new file or section):

```toml
[tool.pytest.ini_options]
markers = [
    "pg: requires MARKETAI_PG_TEST_URL and role fixtures",
    "migration: migration lifecycle tests",
]
```

### 0.4 Create `tests/conftest.py` with shared fixtures

Extract from §5.1.2:

| Fixture | Scope | Description |
|---|---|---|
| `deterministic_clock` | function | Strictly monotonic UTC, +1μs per call |
| `test_workspace` | function | `ws-test-001` with signed-context GUCs on PG |
| `second_workspace` | function | `ws-test-002` for cross-workspace negatives |
| `pg_roles` | session | Connections as each of the 6 roles |
| `schema2_experiment` | function | Schema-2 experiment with 2 entries, review approved |
| `frozen_experiment` | function | Extends above: frozen with Version + Candidates |
| `running_experiment` | function | Extends above: bindings + started |
| `judged_experiment` | function | Extends above: Judgement + Proposal |
| `confirmed_experiment` | function | Extends above: Confirmation + Decisions + projections |
| `canonical_test_vector` | session | Pinned nested object with known canonical JSON + SHA-256 |
| `legacy_experiment` | function | `record_schema_version=NULL` |
| `judge_v1_fixtures` / `judge_v2_fixtures` | session | Pinned snapshot→verdict pairs |

---

## Phase 1: Migration (`p1a2b3c4d5e6`)

### 1.1 Write migration tests first

**File**: `tests/test_migration_issue22.py` (new)

| Tests | §5 ref | Count |
|---|---|---|
| MU-01..MU-10 | §5.8.1 | 10 |
| MD-01..MD-11 | §5.8.2 | 11 |
| MC-01..MC-06 | §5.8.3 | 6 |

All tests initially fail (no migration exists yet).

### 1.2 Write migration

**File**: `migrations/versions/p1_immutable_contextual_decisions.py`

```python
revision = "p1a2b3c4d5e6"
down_revision = "n1a2b3c4d5e6"
```

**8-step structure per approved §4.8:**

| Step | Content |
|---|---|
| 1 | Preflight orphan checks (MC-01..MC-04) |
| 2 | Backfills: `workspace_id`, truth-dependency, QC sequence/hash/terminal |
| 3 | NOT NULL enforcement, INDEX replacement for `frozen_snapshot_hash` |
| 4 | New columns: `record_schema_version`, `started_at`, `decision_schema_version`, `product_profile_entry_id`, `execution_mode`, etc. |
| 5 | New tables (items 1-15): existing §4 tables + addendum items 11-15 (`experiment_timeline_heads`, `experiment_timeline_events`, `experiment_notification_outbox`, `experiment_next_round_records`, `experiment_pattern_candidates`) |
| 6 | `marketai_context_is_current` (already exists; extend if needed), `marketai_reject_update_or_delete` shared trigger function, `marketai_maintain_verdict_projection` SECURITY DEFINER |
| 7 | Triggers (append-only for items 12-15; identity-immutable for item 11; column write-once), RLS ENABLE+FORCE+policy on all new tables, grants per §4.9 + D-S4-03 |
| 8 | Postflight: IS NULL counts = 0; downgrade preconditions |

**New roles in migration** (also in `domain-tenant-roles.sql`):
- `marketai_projection_writer` — SELECT on immutable, SELECT+UPDATE on eligibility, full DML on verdict projections, SELECT on existing
- `marketai_erasure_principal` — EXECUTE on erasure procedure only, zero DML, no BYPASSRLS

**Erasure procedure**: `marketai_erase_workspace(UUID)` owned by `marketai_domain_owner` (BYPASSRLS), SECURITY DEFINER, locked `search_path`, PUBLIC EXECUTE revoked, EXECUTE granted to `marketai_erasure_principal` only.

### 1.3 Add models to persistence layer

**File**: `app/adapters/persistence/models.py` — append after `ExperimentDecisionRecord` (line ~991):

| Model | Table | Notes |
|---|---|---|
| `ExperimentVersionRecord` | `experiment_versions` | Append-only |
| `ExperimentVersionCandidateRecord` | `experiment_version_candidates` | Append-only, has `execution_mode` |
| `ExperimentVersionConceptRecord` | `experiment_version_concepts` | Append-only |
| `ExperimentVersionResolutionRecord` | `experiment_version_resolutions` | Append-only |
| `ExperimentEligibilityEventRecord` | `experiment_eligibility_events` | Append-only |
| `ExperimentEligibilityProjectionRecord` | `experiment_eligibility_projections` | Mutable (UPDATE on head) |
| `DecisionProposalRecord` | `decision_proposals` | Append-only |
| `DecisionConfirmationRecord` | `decision_confirmations` | Append-only |
| `ContextualVerdictProjectionRecord` | `contextual_verdict_projections` | Managed by SECURITY DEFINER |
| `ExperimentTimelineHeadRecord` | `experiment_timeline_heads` | Mutable (`head_sequence` UPDATE) |
| `ExperimentTimelineEventRecord` | `experiment_timeline_events` | Append-only |
| `ExperimentNotificationOutboxRecord` | `experiment_notification_outbox` | Append-only, no delivery columns |
| `ExperimentNextRoundRecord` | `experiment_next_round_records` | Append-only, composite lineage FKs |
| `ExperimentPatternCandidateRecord` | `experiment_pattern_candidates` | Append-only, composite lineage FKs |

### 1.4 Update `domain-tenant-roles.sql`

Add `marketai_projection_writer` and `marketai_erasure_principal` role creation, password parameters, grants. Add addendum table grants for all existing roles.

### 1.5 Verify

```bash
make verify  # MU-01..MU-10, MD-01..MD-11, MC-01..MC-06 pass
```

---

## Phase 2: Domain and service layer

### 2.1 Write domain tests first

**Files** (all new, under `tests/`):

| File | §5 ref | Tests | Count |
|---|---|---|---|
| `test_experiment_lifecycle.py` | §5.4.1 | LC-01..LC-17 | 17 |
| `test_freeze_gates.py` | §5.4.2 | FG-01..FG-20 | 20 |
| `test_eligibility.py` | §5.4.3 | EL-01..EL-12 | 12 |
| `test_proposal_confirmation.py` | §5.4.4 | DP-01..DP-16 | 16 |
| `test_invalidation.py` | §5.4.5 | IV-01..IV-10 | 10 |
| `test_optimistic_concurrency.py` | §5.4.6 | OC-01..OC-08 | 8 |
| `test_schema_dispatch.py` | §5.4.7 | SD-01..SD-07 | 7 |
| `test_lock_order.py` | §5.4.8 | LO-01..LO-05 | 5 |
| `test_immutable_snapshot.py` | §5.4.9 | IS-01..IS-05 | 5 |
| `test_review_coverage.py` | §5.5.1 | RC-01..RC-09 | 9 |
| `test_qc_coverage.py` | §5.5.2 | QC-01..QC-23 | 23 |
| `test_truth_dependency.py` | §5.5.3 | TD-01..TD-13 | 13 |
| `test_candidate_conflict.py` | §5.5.4 | CC-01..CC-13 | 13 |
| `test_post_freeze_validity.py` | §5.5.5 | PFV-01..PFV-05 | 5 |
| `test_bounded_evidence.py` | §5.5.6 | BE-01..BE-05 | 5 |
| `test_acceptance_criteria.py` | §5.2 | AC1-01..AC7-10 | ~75 |
| `test_controlled_mock.py` | §5.10 | MP-01..MP-05, PI-01..PI-07, UB-01..UB-06 | 18 |
| `test_hash_sensitivity.py` | §5.3.4–5 | HD-01..HD-09, ECH-01..ECH-07, SI-01..SI-04 | 20 |

**Total domain tests**: ~281

### 2.2 Implement service layer

**File**: `app/experiments/service.py` — extend existing module with:

| Service function | §3 command | Key logic |
|---|---|---|
| `create_experiment()` | Create | `record_schema_version=2`, advisory warnings |
| `add_entry()` | Add entry | Draft-only guard, cross-workspace AV check |
| `update_entry()` | Update entry | Draft-only guard |
| `remove_entry()` | Remove entry | Draft-only guard |
| `update_context()` | Update context | Draft-only guard |
| `freeze_experiment()` | Freeze | 16-gate pipeline, Version+Candidates+Concepts atomically |
| `bind_candidate()` | Bind | Frozen-only |
| `start_experiment()` | Start | QC/eligibility checks |
| `run_judgement()` | Judge | Pure judge + Proposal creation |
| `confirm_proposal()` | Confirm | Confirmation + Decisions + projections + timeline + outbox |
| `close_experiment()` | Close | State transition with closure_kind |
| `create_next_round()` | Next-round | D-S3-03 contract |
| `create_pattern_candidate()` | Pattern | D-S3-04 contract |

**File**: `app/experiments/eligibility.py` (new) — eligibility event processing, projection fold/rebuild.

**File**: `app/experiments/timeline.py` (new) — timeline head allocation, event append, sequence management.

**File**: `app/experiments/outbox.py` (new) — notification outbox writer, source-dedup, payload canonical JSON.

### 2.3 Verify

```bash
make verify  # All domain tests pass on SQLite
```

---

## Phase 3: API routes

### 3.1 Write API tests first

**File**: `tests/test_experiment_api.py` (new or extend existing)

| Tests | §5 ref | Count |
|---|---|---|
| §5.9.1 full command matrix | AS-01..AS-12 | 12 |
| §5.9.3 pagination | PG-01..PG-09 | 9 |
| §5.9.4 response hashes | RH-01..RH-04 | 4 |
| §5.9.5 ETag/If-Match | ET-01..ET-11 | 11 |
| §5.9.6 addendum endpoints | PA-01..PA-12 | 12 |

**Total API tests**: ~48

### 3.2 Implement API routes

**File**: `app/experiments/api.py` — extend existing router:

| Route | Method | New? |
|---|---|---|
| `GET /api/experiments` | List with keyset pagination | New (§A1) |
| `GET /api/experiments/{id}` | Detail | Exists (update for schema-2) |
| `GET /api/experiments/{id}/timeline` | Timeline | New (§A2) |
| `POST /api/experiments/{id}/entries` | Add entry | New |
| `PUT /api/experiments/{id}/entries/{eid}` | Update entry | New |
| `DELETE /api/experiments/{id}/entries/{eid}` | Remove entry | New |
| `PUT /api/experiments/{id}/context` | Update context | New |
| `POST /api/experiments/{id}/freeze` | Freeze | Exists (update) |
| `POST /api/experiments/{id}/candidates/{cid}/bind` | Bind | New |
| `POST /api/experiments/{id}/start` | Start | New |
| `POST /api/experiments/{id}/judge` | Judge | Exists (update) |
| `POST /api/experiments/{id}/proposals/{pid}/confirm` | Confirm | New |
| `POST /api/experiments/{id}/close` | Close | Exists (update) |
| `POST /api/experiments/{eid}/decisions/{did}/next-round` | Next-round | New (§A6) |
| `POST /api/experiments/{eid}/decisions/{did}/pattern-candidates` | Pattern | New (§A7) |

Shared middleware: `Idempotency-Key` extraction, `If-Match` revision/sequence, schema-version dispatch, permission check via `TenantContext`.

### 3.3 Verify

```bash
make verify  # All API tests pass
```

---

## Phase 4: Worker and notification outbox

### 4.1 Write outbox tests first

**File**: `tests/test_notification_outbox.py` (new)

| Tests | §5 ref | Count |
|---|---|---|
| NO-01..NO-18 | §5.14.3 | 18 |

### 4.2 Implement outbox processing

**File**: `app/experiments/outbox.py` — outbox writer (already created in Phase 2).

**File**: `app/worker_runtime.py` — add `experiment_notification_delivery` job type (if needed for delivery; §A5 is append-only with no delivery columns, so the outbox is a durable intent record, not a queue to process).

### 4.3 Write next-round and pattern tests

**File**: `tests/test_next_round.py` (new) — NR-01..NR-27 (27 tests)
**File**: `tests/test_pattern_candidate.py` (new) — PC-01..PC-22 (22 tests)

### 4.4 Verify

```bash
make verify  # NO-*, NR-*, PC-* tests pass on SQLite
```

---

## Phase 5: PostgreSQL integration (RLS, grants, triggers, erasure, concurrency)

### 5.1 Write PG integration tests

**File**: `tests/test_experiment_decisions_pg.py` (new) — the `domain-rls` CI target.

| Tests | §5 ref | Count |
|---|---|---|
| FK-01..FK-10 | §5.6.1 | 10 |
| PU-01..PU-08 | §5.6.2 | 8 |
| AO-01..AO-2N, AO-M1..M2 | §5.6.3 | dynamic |
| WO-01..WO-12 | §5.6.4 | 12 |
| RL-01..RL-15 | §5.6.5 | 15 |
| RG-01..RG-22 | §5.6.6 | 22 |
| SF-01..SF-15 | §5.6.7 | 15 |
| ER-01..ER-07a | §5.6.8 | 8 |
| DC-01..DC-06 | §5.6.9 | 6 |
| TC-01..TC-05 | §5.6.10 | 5 |
| SN-01..SN-10 | §5.11.7 | 10 |
| ATI-01..ATI-41 (38 active) | §5.14.9 | 38 |
| TD-05..TD-10 (PG-only) | §5.5.3 | 6 |
| QC-22, PU-08 | §5.6.2 | 2 |

**Total PG integration tests**: ~157

### 5.2 Verify on PG

```bash
MARKETAI_PG_TEST_URL=... python -m pytest tests/test_experiment_decisions_pg.py -q
```

All PG tests must **fail** (not skip) when `MARKETAI_PG_TEST_URL` is absent in the `domain-rls` CI job.

---

## Phase 6: Full acceptance and CI

### 6.1 SQLite parity

**File**: `tests/test_sqlite_parity.py` (new or extend)

| Tests | §5 ref | Count |
|---|---|---|
| SQ-01..SQ-05 | §5.7.1 | 5 |

### 6.2 Unfinished-marker scan

```bash
grep -rn 'TODO\|FIXME\|HACK' migrations/versions/p1_*.py tests/test_experiment_*.py tests/test_canonical_json.py tests/test_migration_issue22.py tests/test_notification_outbox.py tests/test_next_round.py tests/test_pattern_candidate.py app/experiments/ app/platform/canonical.py
```

Must return zero matches in Issue #22-touched files.

### 6.3 Issue #22 manifest generation

CI artifact: `issue22-manifest.json` per §5.11.2 schema.

### 6.4 Final gates

| Gate | Command | Must pass |
|---|---|---|
| SQLite/default | `make verify` | Yes |
| PostgreSQL domain-rls | `pytest tests/test_experiment_decisions_pg.py -q` | Yes |
| AC-1..AC-7 | Mapped in `test_acceptance_criteria.py` | Yes |
| §4.11 checks 1..88 | Mapped in `test_migration_issue22.py` + PG tests | Yes |
| Addendum traceability (§5.14) | All addendum test files | Yes |
| No TODO/FIXME/HACK | grep scan | Yes |

---

## File inventory

### New files (23)

| Path | Phase | Purpose |
|---|---|---|
| `app/platform/canonical.py` | 0 | Shared canonical JSON/hash |
| `app/experiments/eligibility.py` | 2 | Eligibility events/projections |
| `app/experiments/timeline.py` | 2 | Timeline head/events |
| `app/experiments/outbox.py` | 2 | Notification outbox writer |
| `migrations/versions/p1_immutable_contextual_decisions.py` | 1 | Migration |
| `pyproject.toml` | 0 | pytest markers |
| `tests/conftest.py` | 0 | Shared fixtures |
| `tests/test_canonical_json.py` | 0 | CE/CD/NL/HD tests |
| `tests/test_migration_issue22.py` | 1 | MU/MD/MC tests |
| `tests/test_experiment_lifecycle.py` | 2 | LC tests |
| `tests/test_freeze_gates.py` | 2 | FG tests |
| `tests/test_eligibility.py` | 2 | EL tests |
| `tests/test_proposal_confirmation.py` | 2 | DP tests |
| `tests/test_invalidation.py` | 2 | IV tests |
| `tests/test_optimistic_concurrency.py` | 2 | OC tests |
| `tests/test_schema_dispatch.py` | 2 | SD tests |
| `tests/test_lock_order.py` | 2 | LO tests |
| `tests/test_immutable_snapshot.py` | 2 | IS tests |
| `tests/test_hash_sensitivity.py` | 2 | HD/ECH/SI tests |
| `tests/test_acceptance_criteria.py` | 2 | AC tests |
| `tests/test_controlled_mock.py` | 2 | MP/PI/UB tests |
| `tests/test_experiment_api.py` | 3 | API contract tests |
| `tests/test_notification_outbox.py` | 4 | NO tests |
| `tests/test_next_round.py` | 4 | NR tests |
| `tests/test_pattern_candidate.py` | 4 | PC tests |
| `tests/test_experiment_decisions_pg.py` | 5 | PG integration tests |

### Modified files (5)

| Path | Phase | Change |
|---|---|---|
| `app/adapters/persistence/models.py` | 1 | 14 new model classes |
| `app/experiments/service.py` | 2 | Extended lifecycle + new commands |
| `app/experiments/api.py` | 3 | 9 new routes, 3 updated |
| `app/worker_runtime.py` | 4 | Outbox job type (if needed) |
| `deploy/postgres/domain-tenant-roles.sql` | 1 | 2 new roles, addendum grants |

---

## Test count summary

| Category | Count |
|---|---|
| Unit (canonical JSON, hashes, properties) | ~45 |
| Domain (lifecycle, gates, eligibility, proposal, etc.) | ~281 |
| API (commands, pagination, ETag) | ~48 |
| Outbox + next-round + pattern | ~67 |
| PG integration (RLS, grants, triggers, FK, erasure, concurrency) | ~157 |
| Migration | ~27 |
| SQLite parity | ~5 |
| **Total** | **~630** |

---

## Dependency graph

```
Phase 0 (infra)
  ├── canonical.py ← no deps
  ├── conftest.py ← canonical.py
  └── pyproject.toml ← no deps

Phase 1 (migration)
  ├── migration file ← models (Phase 0 canonical)
  ├── models.py ← migration
  └── domain-tenant-roles.sql ← migration

Phase 2 (domain) ← Phase 1
  ├── eligibility.py ← models, canonical
  ├── timeline.py ← models
  ├── outbox.py ← models, canonical
  └── service.py ← eligibility, timeline, outbox, judge

Phase 3 (API) ← Phase 2
  └── api.py ← service

Phase 4 (worker/outbox) ← Phase 2
  ├── outbox tests ← outbox.py
  ├── next-round tests ← service.py
  └── pattern tests ← service.py

Phase 5 (PG integration) ← Phase 1 + Phase 2
  └── PG test file ← migration, models, roles

Phase 6 (acceptance) ← all above
  └── Final CI gates
```

---

## Risk register

| Risk | Mitigation |
|---|---|
| Migration too large for single transaction on big data | Backfills use `WHERE ... IS NULL` guards; re-runnable. Scheduled-tier timed test (MU-07: 10K experiments < 10 min). |
| Canonical JSON float divergence across Python versions | Property tests (CD-P1..P4) + pinned `canonical_test_vector` fixture. |
| Deadlock in concurrent confirmation | `threading.Barrier` + `lock_timeout=5s` + SQLSTATE diagnostics. Zero tolerance for `40P01`. |
| Existing tests break from model changes | All new columns are nullable or have defaults. Migration backfills existing rows. `make verify` gate at each phase. |
| `marketai_erasure_principal` / `marketai_projection_writer` missing on existing clusters | `domain-tenant-roles.sql` creates them idempotently. Migration step 6 also creates them. |
