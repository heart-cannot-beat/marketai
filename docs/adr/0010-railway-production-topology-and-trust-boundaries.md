# ADR 0010: Railway production topology and trust boundaries

**Status:** Accepted  
**Date:** 2026-09-22  
**Part of:** #30 (Production readiness), implements #108  
**Supersedes:** `origin/codex/railway-shared-dev` (shared-development topology)

## Context

MarketAI needs a production deployment for the invite-only Beta. The
shared-development topology on `codex/railway-shared-dev` demonstrated the
service separation but used a public MinIO endpoint, a single shared
environment in EU West, and no formal trust boundaries. Production requires
isolated staging and production environments in Singapore, private object
storage, least-privilege runtime identities, and cost control within Railway's
Hobby tier.

The current Railway project is legacy: one production environment in EU West
with Web, public MinIO, and one PostgreSQL instance. It has no API service,
Worker service, Release service, staging environment, Railway Bucket, CI
deployment gate, or health checks. This ADR defines the target topology; the
legacy project will be decommissioned after migration.

## Decision

### Service topology

```
┌─────────────────────────────────────────────────────────────────────┐
│  Railway Project — Singapore Region                                 │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  production environment                                     │    │
│  │                                                             │    │
│  │  ┌──────────┐  private   ┌──────────┐                      │    │
│  │  │   Web    │──────────→│   API    │                      │    │
│  │  │ (public) │           │(private) │                      │    │
│  │  │ :4002    │           │ :8000    │                      │    │
│  │  └──────────┘           └────┬─────┘                      │    │
│  │       │                      │                             │    │
│  │       │                      │ private                     │    │
│  │       │                 ┌────┴─────┐                      │    │
│  │       │                 │  Worker  │                      │    │
│  │       │                 │(private) │                      │    │
│  │       │                 │ no port  │                      │    │
│  │       │                 └────┬─────┘                      │    │
│  │       │                      │                             │    │
│  │       │    ┌─────────────────┼──────────────┐             │    │
│  │       │    │                 │              │             │    │
│  │       │    ▼                 ▼              ▼             │    │
│  │  ┌──────────┐  ┌──────────────────┐  ┌───────────┐      │    │
│  │  │PostgreSQL│  │  Railway Bucket  │  │  Release  │      │    │
│  │  │(private) │  │  (auth-only)     │  │(run-to-   │      │    │
│  │  │          │  │                  │  │completion)│      │    │
│  │  └──────────┘  └──────────────────┘  └───────────┘      │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  staging environment (normally stopped)                     │    │
│  │  Same topology; started for release validation only.        │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

| Service | Exposure | Runtime identity | Dockerfile | Restart | Serverless |
|---------|----------|------------------|------------|---------|------------|
| Web | Public HTTPS | `marketai_control_app` | `Dockerfile.web` | On Failure | Eligible |
| API | Private only | `marketai_domain_api` | `Dockerfile.api` | On Failure | Eligible |
| Worker | Private only | `marketai_domain_worker` | `Dockerfile.worker` | On Failure | Not eligible |
| Release | Private, run-to-completion | schema owners | `Dockerfile.release` (#109) | Never | N/A |
| PostgreSQL | Private only | Railway bootstrap | Railway managed | Always | N/A |
| Railway Bucket | Authenticated HTTPS only; no public-bucket mode | Environment-scoped S3 credential | Railway managed | N/A | N/A |

### Trust boundaries

1. **Only Web has a public application endpoint.** Browser requests cannot
   address API, Worker, PostgreSQL, or Release. A browser can address only an
   exact Railway Bucket object operation for which API issued a short-lived
   signed capability. API and Worker listen on Railway private URLs
   (`*.railway.internal`).

2. **Database role isolation.** API connects as `marketai_domain_api` and
   Worker as `marketai_domain_worker`. Neither role has DDL privileges or
   `BYPASSRLS`. Schema-owner and bootstrap credentials are injected only
   into the Release service (#109).

3. **Control-plane / domain-boundary.** Web receives the control-plane
   `DATABASE_URL` and delegated signing key. API and Worker receive
   `MARKETAI_DATABASE_URL` and the JWKS verification URL. Neither side
   receives the other's database credentials.

4. **Object storage.** Railway Bucket has no public-bucket mode, but its S3
   endpoint is reached over authenticated public HTTPS rather than Railway
   private networking. API and Worker receive only the current environment's
   bucket credential; Release receives it for the mandatory connectivity and
   capability probe. Web receives no bucket credential. Browser uploads and
   downloads use short-lived signed URLs issued by API. This outbound access
   adds no public listener or domain to API, Worker, or Release.

5. **Secret scoping.** Per-service environment variable contracts
   (`deploy/railway/env/*.env.contract`) define REQUIRED, OPTIONAL, and
   DENIED variables. The static deployment validator enforces these
   contracts before deployment.

6. **Meta write disabled.** `META_WRITE_ACCESS_TOKEN` is DENIED in every
   runtime service contract. `META_WRITE_ENABLED` must be `"false"`.
   Production SMTP and Meta Sandbox reads are enabled; real Meta writes,
   automatic ad mutation, and Stripe Live remain disabled.

### Private networking

Application service-to-service traffic uses Railway private URLs; the bucket
is the explicit authenticated-HTTPS exception:

- Web → API: `http://marketai-api.railway.internal:8000`
- Web → PostgreSQL: `postgresql://...@<pg>.railway.internal:5432/marketai_control`
- API → PostgreSQL: `postgresql://...@<pg>.railway.internal:5432/marketai_domain`
- Worker → PostgreSQL: same as API, different role
- API/Worker/Release → Railway Bucket: authenticated TLS to the
  Railway-provided `*.storageapi.dev` endpoint
- API → Web (projection sync): `http://marketai-web.railway.internal:4002`

### Railway Bucket BlobStore contract

The existing `S3BlobStore` remains versioned-S3-only. Railway Bucket does not
support object versioning, lifecycle rules, object locks, configurable
server-side encryption, or automatic bucket snapshots/backups. Its public
documentation covers presigned URLs and ordinary object operations, but does
not specify SHA-256 enforcement/retrieval, ETag construction, conditional-copy
headers, or consistency semantics. See
`docs/research/2026-09-22-railway-bucket-blob-store.md`.

`MARKETAI_BLOB_STORE_ADAPTER=railway_bucket` selects the dedicated
`RailwayBucketBlobStore`. It does not treat ETag as a checksum or pretend an
S3 `VersionId` exists:

1. browser bytes go directly to a server-generated staging key using a
   short-lived presigned PUT;
2. the Worker reads the complete staged object and records its actual size and
   SHA-256 as a content revision;
3. commit reads the then-current staged object into a private temporary file,
   recomputes size and SHA-256, and raises `ObjectChangedError` before any
   committed write if either differs from the inspected revision;
4. only that verified temporary snapshot is sent in one PutObject to the
   committed key; an existing different committed object is never overwritten;
5. read-after-write verification must match before a `CommittedBlobRef` is
   returned, otherwise the adapter deletes the unreferenced destination where
   possible and raises `BlobStoreError`.

This snapshot-and-rehash protocol is the safety path. It does not depend on
Railway accepting checksum headers or conditional copy. The release probe
still records checksum round-trip, ETag overwrite, and stale conditional-copy
behavior as explicit booleans so a future server-side-copy optimization cannot
be enabled from documentation assumptions alone.

### Failure modes, recovery, and ownership

| Failure | Fail-closed behavior | Recovery / owner |
|---------|----------------------|------------------|
| staging missing before commit | `RevisionGoneError`; no committed write | User re-uploads; Worker records terminal rejection |
| staging replaced after inspection | `ObjectChangedError`; no committed write | User re-uploads; race regression test is mandatory |
| existing committed key has different bytes | `BlobStoreError`; never overwrite | Production owner investigates key reuse/corruption |
| Put/read-back is ambiguous or unverifiable | no `CommittedBlobRef`; `BlobStoreError` | Job retries only where safe; operator removes any unreferenced key |
| staging delete fails | cleanup Job remains retryable and crash-recoverable | Worker retries with bounded backoff; operator inspects old terminal intents |
| abandoned or orphan staging object | not handled by bucket lifecycle | Application reconciliation over persisted UploadIntents; production owner reviews ambiguous orphans |
| object loss or bucket deletion | no Railway object-level snapshot restore | Production owner restores from the separately operated asset backup/export; whole-bucket deletion has only Railway's limited restore window |

Staging deletion is an application responsibility. The transaction that marks
an UploadIntent `finalized`, `rejected`, or `expired` enqueues a unique
`cleanup_staging_upload` Job. Deletion is idempotent, and the Job is marked
safe to recover after a Worker lease expires. Cleanup never depends on a
bucket lifecycle rule. Operators own periodic orphan reconciliation and must
never delete a key whose UploadIntent ownership is ambiguous.

The Release service runs `scripts.verify_railway_bucket.py` before migrations
when this adapter is selected. It exercises a real presigned upload,
read/write/read-back/delete, and presigned download generation, then reports
only redacted capability booleans for checksum, ETag, and conditional copy.
It never prints signed URLs, credentials, or secret headers. A core
connectivity/integrity failure aborts the release; an unsupported optional
copy/checksum capability remains visible but does not weaken the rehash path.

### Health checks

| Service | Path | What it proves |
|---------|------|----------------|
| Web | `/api/readiness` | NextAuth session store, Prisma connection |
| API | `/api/health/ready` | Database connection, RLS role, JWKS cache |
| API | `/api/health/live` | Process alive |
| Worker | (none) | Process supervised by Railway; restart On Failure |
| Release | (none) | Exits 0 on success, non-zero on failure |

Dockerfiles include `HEALTHCHECK` directives for Web and API. Railway
uses these for readiness gating and rolling deploys.

### Serverless cold-start contract

Web and API are eligible for Railway Serverless to reduce idle cost.
Worker is not eligible because it polls a durable job queue and must
remain available for time-sensitive job execution.

When Serverless is enabled:

- **Cold-start budget:** Web and API must respond to the health check
  within 15 seconds of cold start (the `--start-period` in the
  Dockerfile `HEALTHCHECK`).
- **JWKS cache warming:** API fetches the JWKS on startup. The cold-start
  budget accounts for this network round-trip.
- **No durable-job safety violation:** A Serverless Web or API instance
  may be stopped after idle timeout. No in-flight domain work is lost
  because all durable work is dispatched to the Worker's job queue and
  persisted in PostgreSQL.

### Staging strategy

- Staging uses the same service topology as production.
- Staging is normally stopped (all services scaled to zero) and started
  only for release validation.
- Staging has its own PostgreSQL instance and Railway Bucket — no shared
  state with production.
- Staging does not require production credentials; it can be fully
  exercised with controlled-mock AI providers and Meta Sandbox.
- The Release service (#109) runs in staging first to validate migrations
  before production deployment.

### Cost profile

Target: approximately US$5/month with a US$20/month hard ceiling under
Railway's Hobby plan.

| Component | Idle cost | Active cost |
|-----------|-----------|-------------|
| Web (Serverless) | $0 | Pay-per-use |
| API (Serverless) | $0 | Pay-per-use |
| Worker (always-on) | ~$2/month | ~$3/month |
| PostgreSQL | ~$1/month | ~$2/month |
| Railway Bucket | ~$0 | Usage-based |
| Staging (stopped) | $0 | Same as production when running |

Serverless for Web and API is critical to staying within the Hobby
budget. The Worker runs continuously because job polling cannot tolerate
cold starts.

## Consequences

- The legacy Railway project (EU West, public MinIO) must be
  decommissioned after the Singapore project is operational.
- Railway Bucket storage is exercised by the Release service before database
  migration; production refuses any other blob-store adapter.
- Browser-to-bucket traffic and API/Worker/Release bucket traffic use the
  authenticated public S3 endpoint. API, Worker, PostgreSQL, and Release still
  have no public inbound application endpoint.
- Versionless commit downloads the staged object once more and uploads the
  verified snapshot. This costs Worker bandwidth and temporary disk, but keeps
  the byte-identity guarantee independent of undocumented ETag/copy behavior.
- Backup, PITR, restore-drill, and RPO/RTO are not covered by this ADR.
  They belong to #109 (Release service) and the broader #30 production
  readiness scope.
- Single-region, single-replica: no HA or multi-region. A Railway
  infrastructure incident in Singapore takes the site offline. This is
  acceptable for the invite-only Beta.
- Adding a new runtime service requires a new environment contract file
  and re-running the static validator.
