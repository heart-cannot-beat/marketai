# Railway Beta Operations Runbook

## Ownership and Response Targets

| Field | Value |
|---|---|
| Owner | Repository owner (see `RAILWAY_ALERT_EMAIL`) |
| Business hours | Hong Kong (HKT, UTC+8) |
| Critical incident ack | 30 minutes |
| Critical incident recovery | 4 hours |
| On-call commitment | None (Beta) |

## Environment Variable Contract

The following environment variables must be configured in the Railway
deployment.  Issue #108 integrates these into the Railway service topology.

### Worker

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `WORKER_DRAIN_TIMEOUT_SECONDS` | No | `300` | Seconds Railway waits after SIGTERM before SIGKILL. Must match or exceed `JOB_LEASE_SECONDS`. |
| `JOB_LEASE_SECONDS` | No | `300` | Heartbeat lease window for claimed jobs. |
| `JOB_POLL_SECONDS` | No | `0.5` | Idle-tick interval between job claims. |
| `MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL` | Yes | (empty) | Web's private URL for onboarding-fact delivery; the same value as the API's. Reference `${{marketai-api.MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL}}`. |
| `DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN` | Yes | (empty) | Bearer token for onboarding-fact delivery; must equal the Web's. Reference `${{marketai-web.DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN}}`. |
| `MARKETAI_PROJECTION_SYNC_TIMEOUT_MS` | No | `3000` | HTTP timeout for each onboarding-fact delivery. |

### JWKS / Key Rotation

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `JWKS_REVOKED_KEY_IDS` | No | (empty) | Comma-separated key IDs to reject immediately, even if still in JWKS response. |
| `DELEGATED_JWKS_URL` | Yes (prod) | (empty) | Control-plane JWKS endpoint. |
| `DELEGATED_JWKS_MAX_STALE_SECONDS` | No | `600` | Hard-stale cache limit. |
| `DELEGATED_JWKS_REFRESH_INTERVAL_SECONDS` | No | `300` | Background refresh interval. |
| `DELEGATED_JWKS_INITIAL_LOAD_ATTEMPTS` | No | `20` | Startup-only retries for the first JWKS fetch (Web may still be starting). |
| `DELEGATED_JWKS_INITIAL_LOAD_BACKOFF_SECONDS` | No | `2` | First retry delay; doubles each attempt. |
| `DELEGATED_JWKS_INITIAL_LOAD_MAX_BACKOFF_SECONDS` | No | `20` | Cap on the retry delay. Defaults total ~5.5 min; keep the API's Railway healthcheck timeout (Settings → Deploy) at 600s so the window outlasts the budget. |

### Retention

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `RETENTION_APP_LOG_DAYS` | No | `30` | Application log retention. |
| `RETENTION_SECURITY_LOG_DAYS` | No | `90` | Security and audit log retention. |
| `RETENTION_TRACE_DAYS` | No | `7` | Trace retention. |

### Spend Guardrails

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MARKETAI_PLATFORM_MAX_PER_OPERATION` | No | `5` | Hard per-operation spend limit (USD). |
| `MARKETAI_PLATFORM_MAX_ROLLING_24H` | No | `20` | Rolling 24-hour spend limit. |
| `MARKETAI_PLATFORM_MAX_PERIOD` | No | `100` | Per-period spend limit. |
| `MARKETAI_PLATFORM_MAX_OUTSTANDING` | No | `10` | Maximum outstanding (unreleased) spend. |

These are the ceilings an Owner cannot raise a Workspace past. Provisioning
creates each new Workspace's guardrail at the Beta defaults (5 / 20 / 100 / 10
in its budget currency, `app/entitlements/guardrail_defaults.py`), which equal
the ceilings above, so with the defaults an Owner can lower a limit in
Settings and raise it back, but not above the default. A Workspace provisioned
before migration `zzg126a1b2c3` keeps its 0 limits until its Owner raises them
there (#126).

### Railway Bucket

Use the service-specific reference templates in `deploy/railway/env/`. The
`Bucket` reference name must match the Railway canvas service. `BUCKET` is the
S3 API name; do not substitute `RAILWAY_BUCKET_NAME`, which is only the display
name. API and Worker receive environment-scoped bucket credentials but never
schema-owner database credentials. Web receives neither.

Railway Bucket is access-controlled but currently reached through its public
HTTPS S3 endpoint. This is outbound authenticated traffic and does not add a
public domain or listener to API, Worker, Release, or PostgreSQL.

Every Release runs `scripts.verify_railway_bucket.py` before migrations. A
presigned upload/read-back/delete failure aborts release. Checksum, ETag, and
stale conditional-copy results are printed only as named booleans; `false`
means the adapter continues to use the Worker snapshot-and-rehash protocol.
Never paste a signed URL or credential into a ticket or log.

API and Worker must run `MARKETAI_BLOB_STORE_ADAPTER=railway_bucket`. The
`s3` adapter ignores `MARKETAI_BLOB_STORE_S3_ENDPOINT_URL` and signs every
grant against `*.s3.amazonaws.com`, so the Worker can still commit while
every browser transfer fails -- the gallery shows "Load failed" on each card.

#### Browser transfers (gallery, lightbox, reference uploads)

The browser fetches signed GET/PUT URLs from the bucket directly, which needs
two things that are easy to miss because nothing server-side exercises them:

1. **Web: `NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS`** -- the exact https origin
   that appears in the signed URLs, `https://<BUCKET>.t3.storageapi.dev`
   (virtual-hosted style; `<BUCKET>` is the S3 API name from the bucket's
   Credentials tab). It feeds the CSP `connect-src` and the browser
   `StorageOriginPolicy`. Because it is a `NEXT_PUBLIC_*` value it is inlined
   at `next build`: `deploy/railway/Dockerfile.web` declares it as a build
   argument, so set the variable **before** the Web image is (re)built --
   a value added after the build only reaches the CSP, and the client still
   refuses the origin ("Transfer failed").
2. **Release: `MARKETAI_BLOB_STORE_BROWSER_ORIGINS`** -- the Web `APP_URL`
   (`${{Web.APP_URL}}` in `release.env.template`). Each Release writes the
   bucket CORS rule (`GET`/`HEAD`/`PUT`, exposes `ETag`) for those origins and
   reads it back; a bucket that does not retain the rule fails the release.
   Without it the Release prints a warning and the browser's bucket requests
   fail with a bare network error.

Staging cleanup is application-owned:

1. Terminal or expired UploadIntents enqueue `cleanup_staging_upload` in the
   same database transaction.
2. The Job waits until the upload capability expires, then deletes the staging
   key idempotently.
3. Storage failures remain retryable and are recovered by the normal Job lease
   mechanism.
4. During operations review, reconcile old `staging/` objects against
   persisted UploadIntents. Do not delete ambiguous orphans until ownership is
   established.

Railway supplies no lifecycle rules or object-level automatic backups. A lost
committed object requires the separately operated asset backup/export path;
the whole-bucket deletion restore window is not an object backup.

#### Gallery thumbnails (generated assets)

The Worker commits a 512px-long-edge JPEG beside every generated original
(`app/generation/thumbnails.py`) under
`committed/<workspace>/generation_thumbnail/<sha256>.jpg`; the gallery and
Run-page tiles load that derivative through a short-lived signed link the
list projections embed, and fall back to the original (download-intent path)
when a version has none. A thumbnail that cannot be derived or committed is
logged as `asset_thumbnail_unavailable` and left null -- it never fails the
RunItem, and the original stays the only truth. Asset Detail, the Lightbox
and exports always use the original.

AssetVersions committed before this derivative existed keep loading the
original until they are backfilled. The backfill is idempotent, resumable
and per Workspace; the API/Worker images do not ship `scripts/`, so run the
module form inside either container:

```bash
railway ssh --project <project> --environment <env> --service <marketai-worker>
python -m app.generation.thumbnail_backfill --workspace-id <workspace uuid> --dry-run
python -m app.generation.thumbnail_backfill --workspace-id <workspace uuid>
```

`--dry-run` only counts candidates. The JSON report lists `derived`,
`failed` (left null, retried by the next run) and `with_thumbnail_before`;
a second run over a finished Workspace derives nothing. Pass
`--workspace-id` once per Workspace (repeatable) -- the Workspace registry
is under RLS, so there is no "all Workspaces" mode. `--limit N` caps a
cautious first pass; the next run continues from the rest. Locally the same
entry point is `python scripts/backfill_asset_thumbnails.py`.

## Service Architecture (Beta)

- **1 Web replica** (Next.js) — public HTTPS UI and BFF; configure
  `DOMAIN_API_BASE_URL=http://${{marketai-api.RAILWAY_PRIVATE_DOMAIN}}:8000`.
  The Web also needs `MFA_ENCRYPTION_KEY` (an independent base64 32-byte
  AES-256 key, `openssl rand -base64 32`) protecting TOTP seeds at rest;
  without it the Security page reports "MFA encryption is not configured" and
  no Owner can enroll MFA -- and `NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS` (see
  "Browser transfers" above), set before the image is built.
- **1 API replica** (FastAPI, Uvicorn) — `/api/health/ready` for health checks.
- **1 Worker replica** (concurrency 1) — no health-check HTTP; liveness via lease heartbeats.
- **1 entitlement dispatcher cron service** — use `Dockerfile.web`, override
  start command to `npm run ops:dispatch-entitlements`, set the UTC cron schedule
  to `*/5 * * * *`, and use `deploy/railway/env/dispatcher.env.template`.
  Do not add a public domain or Web healthcheck. Railway's minimum cron interval
  is five minutes, and an overlapping run is skipped; inspect its deployment
  history after setup to confirm successful exits (see
  [Railway cron jobs](https://docs.railway.com/cron-jobs)).
- **PostgreSQL** — Railway-managed or external.
- **Scaling** — manual during Beta.

The Worker is a separate private service, not an extra Web or API replica.
Use `deploy/railway/Dockerfile.worker` and a database URL with the
`marketai_domain_worker` role. Never reuse the API role, control-plane database
URL, or schema-owner credentials. The dispatcher uses the control-plane app
database role; it needs no session secret, delegated signing key, Bucket
credential, or public listener.

The Worker alone delivers onboarding facts (the first-value items of the
Generate page's checklist) to the Web. It needs
`MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL`, the same Web private URL the API
uses, and `DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN`, equal to the Web's value. Set
both as the references in `deploy/railway/env/worker.env.template` so neither
drifts from its source. With either empty, the Worker skips delivery without
an error; its startup log then shows
`onboarding_delivery_config base_url_set=… token_set=…`, naming the empty one.
Both are read once at startup, so redeploy the Worker after changing them.
Undelivered facts wait in the outbox: once both are set, the Worker delivers
the backlog oldest first, and the Web keeps each fact's original time and
deduplicates by event id, so nothing needs replaying by hand. General-lane jobs
start slightly later while the backlog drains. A failed delivery logs
`onboarding fact delivery failed event_id=… reason=…`:
`reason=control plane returned HTTP 403` means the Web's token is empty or
differs, and `reason=control plane request failed: <error class>` means the
Worker could not reach the base URL.

For signed entitlement delivery, the Web's
`ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID` must appear in the API's
`ENTITLEMENT_VERIFICATION_KEY_RING`. Each API ring entry is
`key-id:hex-encoded-key-bytes`; the Web signs with the UTF-8 bytes of
`ENTITLEMENT_SNAPSHOT_SIGNING_KEY`. Convert those same bytes to hex when
configuring the API. Do not log or paste the key into tickets. A `400` from the
inbox can move an outbox entry to `DEAD_LETTER`; after fixing the ring, publish
a fresh snapshot through the normal operator publication path and confirm that
the dispatcher delivers it. Do not edit outbox rows directly.

`PROVISIONING` is not equivalent to an activated Workspace. Activation still
requires a delivered snapshot and verified Owner MFA. The Owner must complete
authenticator enrollment personally; operations must not bypass that gate.
Enrollment is only possible once `MFA_ENCRYPTION_KEY` is set on the Web
service, so verify it before inviting the first Owner.

## Graceful Shutdown

1. Railway sends `SIGTERM` to the Worker process.
2. The Worker sets `stopping = True` and logs `SIGTERM received, draining in-flight work`.
3. The current `run_once()` completes — the in-flight job finishes normally.
4. The Worker logs `Worker drain completed` with elapsed time and exits.
5. If the drain exceeds `WORKER_DRAIN_TIMEOUT_SECONDS`, Railway sends `SIGKILL`.
6. SIGKILL recovery: the heartbeat stops, the lease expires, and `recover_expired_jobs()` on the next idle tick classifies the job:
   - **Leased** (not yet dispatched): requeued safely.
   - **Running + safe_to_retry_after_crash**: requeued.
   - **Running** (paid call dispatched): moved to `uncertain`.

## Uncertain-State Handling

An `uncertain` job:
- Is visible in the Operation state projection.
- Emits a `CRITICAL` structured alert (`category: uncertain_state`).
- Blocks unsafe automatic replay.
- Must be resolved by the owner via manual reconciliation or the recovery matrix.

The owner is notified within 24 hours (via Railway log-based alerts matching
`"severity":"critical"` + `"category":"uncertain_state"`).

## JWKS Key Rotation

### Scheduled Rotation (every 90 days)

1. Control plane generates a new signing key and publishes both old and new
   keys in the JWKS response (overlap window).
2. Domain services refresh their JWKS cache and accept tokens signed by
   either key.
3. After the overlap window (>= `DELEGATED_JWKS_MAX_STALE_SECONDS`), the
   control plane removes the old key from the JWKS response.
4. Existing sessions signed with the old key expire naturally (max TTL 60s).

### Emergency Revocation

1. Add the compromised key ID to `JWKS_REVOKED_KEY_IDS` in Railway env vars.
2. Redeploy the API and Worker services — the key is rejected immediately on
   startup, before any JWKS refresh.
3. The control plane removes the compromised key from its JWKS response.
4. Issue a new signing key and resume normal rotation.

**Rehearsal:** Test emergency revocation quarterly by adding a test key ID to
`JWKS_REVOKED_KEY_IDS`, verifying rejection, and removing it.

## BYOK Credential Compromise

1. The Workspace Owner rotates the compromised credential in their provider
   dashboard.
2. The Owner updates the credential in MarketAI (KMS-managed
   `CredentialVersion`).
3. In-flight jobs using the old credential will fail or go `uncertain`;
   resolve via the recovery matrix.
4. The old `CredentialVersion` is marked revoked — it cannot be used for new
   operations.

## External Provider Outage

1. In-flight jobs retry per their `max_attempts` and backoff schedule.
2. Jobs that exhaust retries go to `failed`; ambiguous outcomes go to
   `uncertain`.
3. New operations are admitted but queue behind the failing jobs.
4. The Owner is notified via reconciliation alerts.
5. Once the provider recovers, `uncertain` jobs are resolved via manual
   reconciliation.

## Budget Exhaustion

1. The `ReservationBook` checks all four guardrail formulas atomically
   before reserving spend.
2. If any limit is exceeded, the operation is rejected with
   `GuardrailExceededError`.
3. If the guardrail row is missing or the database is unreachable, the
   operation fails closed (`NoResultFound` or connection error propagates).
4. The Owner adjusts guardrails via the spend management UI.
5. Workspace guardrails default to zero until an Owner configures them.

## Retention and Redaction

### Retention Boundaries

| Category | Retention | Deletable |
|---|---|---|
| Application logs | 30 days | Yes |
| Security logs | 90 days | Yes |
| Traces | 7 days | Yes |
| Audit events | Indefinite | **No** (protected) |
| Cost ledger | Indefinite | **No** (protected) |
| Launch audit log | Indefinite | **No** (protected) |
| Verdict decisions | Indefinite | **No** (protected) |
| Library audit | Indefinite | **No** (protected) |

### Redaction

All structured alerts are emitted through `ops_alerts.emit()`, which:
- Strips tokens, credentials, and Meta access tokens from all string fields.
- Redacts dictionary keys matching secret patterns.
- Truncates any field to 500 characters.
- Never includes raw provider payloads.

## Alert Categories

Configure Railway log-based alerts to match on the `"alert": true` JSON
field, keyed by severity and category:

| Severity | Category | Trigger |
|---|---|---|
| `critical` | `uncertain_state` | A job's paid-call outcome is unknown. |
| `warning` | `reconciliation` | Lease recovery found uncertain jobs. |
| `info` | `reconciliation` | Lease recovery or operation fold completed. |
| `info` | `shutdown` | Worker drain started or completed. |
| `info` | `retention` | Retention sweep deleted operational artifacts. |
| `critical` | `scale_evaluation` | A Scale evaluation run holding its Episode stayed unfinished after two redrives; the Episode gets no new live run until it is resolved. |

## Recovery Signals

| Signal | Meaning | Action |
|---|---|---|
| `/api/health/ready` returns 503 | Database or JWKS cache unhealthy | Check DB connectivity and JWKS endpoint. |
| No heartbeat renewal | Worker process dead or frozen | Check Railway logs; the lease will expire and recovery will classify the job. |
| `uncertain_state` alert | Paid call outcome unknown | Consult recovery matrix; do not auto-retry. |
| Reconciliation alert with `uncertain > 0` | Lease-expired jobs could not be safely retried | Same as above. |
| `scale_evaluation` alert | The run named in `details.evaluation_run_id` holds its Episode (`phase_run_id`) and every `scale_evaluate` Job for it, two redrives included, ended without finishing it | Read the failed Jobs' `error_detail` for the cause and fix it; then enqueue the run once more (`enqueue_scale_evaluation` with the next `redrive` number). The next collection then requests the Episode's live run again. |
