# Secret-Safe Cross-Area Golden-Path Browser E2E Design

- Status: Draft
- Issue: [#98](https://github.com/Raybbish/marketai/issues/98)
- Refines: [#60](https://github.com/Raybbish/marketai/issues/60)
- Primary contract: `docs/product/v9-functional-alignment-acceptance.md` Section 4
- Completed prerequisites: #57, #23, #27, #28, #87

## 1. Overview

One serial Playwright test proving the complete v9 Beta golden path on PostgreSQL: two-phase provisioning, invitation/MFA, empty Workspace, Library, Generation with deterministic Worker SIGKILL and recovery, Assets review, Experiments freeze/PAUSED Saga, inconclusive then confirmed Winner, Localization truth/items, Scale advice/draft, full restart, and canonical snapshot equality.

### 1.1 Non-Negotiable Invariants

- `storageState: undefined` — real invitation/signup/MFA from raw browser.
- Zero business-table seeding — every domain entity originates from a UI action.
- Two-phase control-plane provisioning preserving ADR 0003 ownership seam.
- Deterministic Worker SIGKILL at the after-remote-commit/before-local-ack window.
- Three-layer evidence: browser NetworkSpy (fail-closed), persisted call evidence (non-vacuous), effective runtime config (boolean-only probes).
- Internal-only Compose network with verified no dual-homing, no egress.
- Secret-safe CI artifacts — no traces, no video, no raw credentials in logs.
- Durable AcceptanceManifest outliving the 90-day diagnostic bundle.

### 1.2 Seeding Boundary

- **Forbidden**: Playwright fixtures, Python scripts, or test code directly calling Prisma/SQL to construct state.
- **Allowed**: Recognized TypeScript control-plane commands using Prisma internally as an implementation detail of the control-plane module.
- **Required**: All domain state arrives only through the entitlement publication seam. The provisioner never writes the domain DB.
- **Required**: All business entities are created through UI actions starting from Step 1.

## 2. Directory Structure

```
web/
├── lib/
│   └── control-plane-provisioning/
│       ├── index.ts                  # sole public interface
│       ├── begin.ts                  # internal: operator, plan, PROVISIONING team, invitation
│       ├── complete.ts               # internal: confirm membership → provision → activate → publish
│       └── types.ts                  # BeginProvisioningResult, CompleteProvisioningResult
├── scripts/
│   ├── provision-workspace.ts        # production operator CLI adapter (two-phase)
│   └── golden-path-provisioning.ts   # E2E command adapter (calls same deep module)
└── e2e/
    ├── golden-path/
    │   ├── golden-path.spec.ts       # single test() with test.step() for Steps 0–10
    │   ├── playwright.config.golden.ts
    │   ├── fixtures/
    │   │   ├── index.ts              # sole public interface: composed test + expect
    │   │   ├── onboarding-harness.ts
    │   │   ├── provisioning.ts
    │   │   ├── workspace-setup.ts
    │   │   ├── service-control.ts
    │   │   ├── controlled-provider-fixtures.ts
    │   │   ├── meta-scenario-fixtures.ts
    │   │   ├── evidence-inspector.ts
    │   │   ├── runtime-safety.ts
    │   │   └── area-drivers/
    │   │       ├── library.ts
    │   │       ├── generation.ts
    │   │       ├── assets.ts
    │   │       ├── experiments.ts
    │   │       ├── localization.ts
    │   │       └── scale.ts
    │   ├── artifacts/
    │   │   ├── safe-capture.ts       # positive-allowlist screenshot with masking
    │   │   ├── evidence-recorder.ts  # cumulative evidence, timing, report generation
    │   │   └── schemas.ts            # TypeScript types consuming canonical JSON Schemas
    │   └── scripts/
    │       ├── inspect_db.py         # read-only domain evidence queries
    │       └── wait_adapter_checkpoint.py  # read-only sidecar checkpoint polling
    ├── library/suite-contract.ts
    ├── generation/suite-contract.ts
    ├── assets/suite-contract.ts
    ├── experiments/suite-contract.ts
    ├── localization/suite-contract.ts
    └── scale/suite-contract.ts

test-support/
├── controlled-provider/
│   ├── Dockerfile
│   ├── server.*                      # HTTP server with provider + test-control interfaces
│   ├── ledger.*                      # transactional SQLite effect ledger
│   └── schemas.*                     # request/response schemas
├── lifecycle-broker/
│   ├── broker.*                      # host-side lifecycle broker (Node.js or shell)
│   ├── protocol.ts                   # shared JSON-over-UDS message types
│   └── Dockerfile                    # optional: sidecar image for CI
└── mail-sink/
    ├── server.*                      # in-memory SMTP + HTTP test-control
    └── Dockerfile

contracts/e2e/
├── golden-path-report.schema.json
├── golden-path-evidence.schema.json
├── acceptance-manifest.schema.json
└── artifact-allowlist.schema.json

scripts/ci/
├── validate_topology.py
├── redact_compose_logs.py
└── verify_golden_artifacts.py

.github/workflows/
├── golden-path-pr.yml
└── golden-path-acceptance.yml

docker-compose.golden-path.yml
```

## 3. Compose Topology

### 3.1 Overlay Configuration

`docker-compose.golden-path.yml` extends `docker-compose.yml`. It adds the controlled-provider sidecar, a Playwright service, and an internal-only network. It does not hardcode a project name — the project name is supplied at invocation:

```
docker compose -p gp-<run_id>-<run_attempt> \
  -f docker-compose.yml \
  -f docker-compose.golden-path.yml up -d
```

All services attach to a single named network with `internal: true`. No service publishes ports to the host. Playwright connects to `http://web:<port>` within the internal network.

A separate local-debug profile may publish the web port for manual troubleshooting, but it cannot be used for PR or acceptance runs.

### 3.2 Services

| Service | Role | Health check |
|---|---|---|
| postgres | PostgreSQL 16 | `pg_isready` |
| init-domain | Alembic domain migrations | exit 0 |
| init-control | Prisma migrations + signing key bootstrap | exit 0 |
| web | Next.js control plane | `/api/auth/providers` |
| api | FastAPI domain API | `/api/health/ready` |
| worker | Python worker | heartbeat or harmless queue probe |
| controlled-provider | Mock provider sidecar | `/test/health` |
| mail-sink | In-memory SMTP capture sink | `/health` |
| playwright | Playwright test runner | test completion |

The Worker's readiness must be proven with a heartbeat or harmless queue probe, not only container health.

### 3.3 Adapter Configuration

The overlay overrides environment for API and Worker:

```
GENERATION_ADAPTER=controlled_mock
CONTROLLED_PROVIDER_URL=http://controlled-provider:<port>
META_ADAPTER=fake_meta
META_WRITE_ENABLED=false
META_WRITE_ACCESS_TOKEN=
```

No real provider credentials are injected. The Generation controlled adapter does not resolve or record provider credentials. `META_ADAPTER=fake_meta` selects the database-backed FakeMetaAdapter at the process level. The `customer_sandbox` evidence mode is **not** an environment variable — after activation, the Owner creates a Workspace-bound Meta Sandbox connection through the normal SaaS UI and same-origin BFF in Step 0.9. This connection carries the immutable `(workspace_id, meta_mode)` provenance pair enforced by ADR 0009. Runtime probes verify the effective booleans and selected adapter kind; the evidence inspector verifies the Workspace connection's `customer_sandbox` provenance only after the connection exists (Section 6.8 and Step 0.10).

### 3.4 Lifecycle Broker

Source: `test-support/lifecycle-broker/`

Playwright runs inside the internal Compose network and cannot access Docker to manage containers. A narrow host-side lifecycle broker provides allowlisted operations for the validated project:

```
stop(web | api | worker)
start(web | api | worker)
kill(worker, SIGKILL)
restart(web | api | worker)
status(web | api | worker)
inspect_network(web | api | worker)
probe_egress(web | api | worker | controlled-provider)
```

**IPC transport**: The broker listens on a Unix domain socket (UDS) at a path mounted into the Playwright container as a bind-mount volume (e.g., `/var/run/lifecycle-broker.sock`). The socket file is created by the broker process on the host before `docker compose up`. Only the Playwright container receives the mount; no other service accesses it. The socket is removed during Compose teardown.

**Protocol**: JSON-over-UDS with a closed request/response schema defined in `test-support/lifecycle-broker/protocol.ts`. Each request contains `{ action, service, signal?, projectName }`. Each response contains `{ ok, service, state?, network_attestation?, egress_attestation?, error_code? }`. The broker validates the project name against the expected `gp-<run_id>-<run_attempt>` pattern and rejects unrecognized values. `inspect_network` returns only the service name, expected network name/hash, attachment count, and invariant booleans; it never returns arbitrary `docker inspect` output. `probe_egress` executes a fixed image-local probe command and returns only DNS/IPv4/IPv6 success booleans; callers cannot supply a command or destination.

**Permissions**: The broker process runs on the host with Docker group membership. Lifecycle and network-inspection requests validate against the closed `RuntimeService = 'web' | 'api' | 'worker'` union; egress probes validate against `EgressProbeService = RuntimeService | 'controlled-provider'`. The broker also validates the project name and rejects every unrecognized action/service combination. The Playwright container has no Docker socket, Docker CLI, or host networking.

**Readiness**: The broker signals readiness by writing to the UDS. `service-control.ts` waits for the socket to become connectable before proceeding. Bounded connection timeout on startup.

**Cleanup**: The broker process is stopped and the socket file removed during the `if: always()` Compose teardown step.

Container resolution uses `docker compose ps -q <service>`, not generated container names. Closed action-specific service unions prevent arbitrary string service names.

### 3.5 Topology Validation

Topology validation runs **host-side** before the Playwright container starts, because Playwright inside Compose cannot execute `docker compose config` or `docker inspect`. Pass 1 runs before `up -d`; Pass 2 necessarily runs after `up -d` and before `run --rm playwright`.

**Pass 1 — Merged configuration** (`docker compose config`, host-side):
Reject any service or network configuration containing:
- `network_mode: host`
- any published host port
- more than one attached network per service
- a network without `internal: true`
- Docker-socket mounts
- host-gateway aliases
- privileged containers
- unapproved host-path mounts

**Pass 2 — Live container inspection** (`docker inspect`, host-side):
Confirm the same invariants hold on the running containers after `up -d` completes but before `run --rm playwright`.

Both passes must succeed. On failure, the CI step exits before launching the Playwright container. The validator does not emit a final attestation until Pass 2 has inspected every required running container.

**Pass 3 — Attestation delivery**: After Pass 2 succeeds, the host-side validator writes a signed topology attestation JSON to a read-only bind-mount shared with the Playwright container. The attestation contains: validated project name, service list, network configuration hash, Compose start timestamp, validation timestamp, pass/fail per invariant, signature algorithm, and signature. The runner generates a per-run Ed25519 keypair in restrictive temporary files. Only the host validator can read the private key; Compose mounts only the public key read-only into Playwright. The private key is never placed in the attestation, containers, environment dumps, logs, or artifacts, and all key files are deleted during `if: always()` teardown. `runtime-safety.ts` verifies the signature, project name, network hash, and that validation occurred after the recorded Compose start timestamp. If the attestation is missing, malformed, stale, incorrectly signed, or contains any failure, the test aborts before any business action.

After a Web/API/Worker restart, `runtime-safety.ts` calls the broker's closed `inspect_network` operation for that service and validates the same live attachment invariants. The initial attestation is not reused as proof for a replaced container.

### 3.6 Egress Verification

Multi-service, multi-protocol egress probes run against web, api, worker, playwright, and the controlled-provider sidecar. Playwright probes its own container directly; for the other containers it asks the lifecycle broker to execute the closed `probe_egress` operation. No Docker socket or arbitrary command surface is exposed to Playwright. Each probe tests:
- external DNS resolution
- direct IPv4 connection
- direct IPv6 connection

Probes use the runtime already present in each image (no assumption of `curl`). They contain no credentials or business data. Any successful external connection fails the test.

## 4. Controlled-Provider Sidecar

Source: `test-support/controlled-provider/`

A lightweight HTTP server with two interfaces distinguished by URL prefix.

### 4.1 Provider Interface

```
POST /provider/generate
POST /provider/reconcile
```

`POST /provider/generate` — Accepts generation requests. Enforces idempotency by `(namespace, idempotency_key)`:
- Same key + same fingerprint → returns the original effect (no new generation).
- Same key + different fingerprint → returns 409.
- At most one successful effect for `(namespace, run_item, effect_kind, request_fingerprint)`.

Simulated output is derived from the request fingerprint plus scenario version, not a global fixed seed.

`POST /provider/reconcile` — Accepts a request body with the idempotency key. Returns the committed effect if found, or 404.

### 4.2 Test-Control Interface

```
POST   /test/runs/:namespace/barriers
POST   /test/runs/:namespace/barriers/:name/arrive
GET    /test/runs/:namespace/checkpoints/:name
DELETE /test/runs/:namespace/barriers/:name
GET    /test/runs/:namespace/effects?limit=<bounded>
GET    /test/runs/:namespace/request_attempts?idempotency_key_hash=<hash>&request_kind=<generate|reconcile>&limit=<bounded>
```

**Barrier registration** (`POST .../barriers`): Registers a barrier keyed by `{ runId, matchOrdinal }`. The sidecar atomically binds the first matching call to the barrier and records its actual opaque RunItem reference. Only `matchOrdinal` is used for matching — not unstable item indexes.

**Barrier arrival** (`POST .../barriers/:name/arrive`): Called by the controlled adapter client inside the Worker after `/provider/generate` returns, but before control returns to the Worker handler. Tests never call this endpoint.

**Checkpoint polling** (`GET .../checkpoints/:name`): Returns checkpoint status. The required terminal state is:
```json
{
  "phase": "after_remote_commit",
  "remote_effect_committed": true,
  "response_received_by_adapter": true,
  "worker_barrier_waiting": true
}
```

**Effects inspection** (`GET .../effects`): Namespaced and bounded. Returns only idempotency-key hashes and opaque effect references — no request bodies, provider payloads, raw credentials, or unbounded ledger dumps.

**Request attempts** (`GET .../request_attempts?idempotency_key_hash=<hash>&request_kind=<generate|reconcile>&limit=<bounded>`): Returns a separate append-only log of inbound provider calls, recorded before generation idempotency deduplication or reconciliation lookup. Each entry contains the namespace, `idempotency_key_hash`, `request_kind`, an opaque request fingerprint, a monotonic sequence number, and a timestamp. Both filters are required for the recovery assertions: `request_kind=generate` proves that the original effect was dispatched exactly once, while `request_kind=reconcile` proves that recovery reconciled rather than blindly redispatching. The endpoint rejects a missing or unknown `request_kind`; an unfiltered cross-kind count is never used for the "no second dispatch" assertion. This log is distinct from the committed effects ledger: effects record what was committed (deduplicated), while request attempts record what reached each provider operation.

### 4.3 Deterministic SIGKILL Sequence

The barrier fires after the adapter receives the response:

1. Worker commits the attempt, reservation, provider idempotency key, and `dispatch_started` evidence locally.
2. Worker calls the controlled-provider sidecar outside the database transaction.
3. Sidecar atomically records the remote effect and returns success.
4. The controlled adapter client receives and validates the response.
5. The controlled adapter client calls `POST .../barriers/:name/arrive` on the sidecar and blocks.
6. `wait_adapter_checkpoint.py` polls until the checkpoint reports the terminal state above.
7. The test sends `SIGKILL` to the Worker via the lifecycle broker.
8. The barrier connection disappears, but the sidecar's effect ledger and checkpoint persist.
9. After the observed lease deadline expires (not immediately), the Worker is restarted.
10. Recovery looks up the persisted provider idempotency key, reconciles the already-committed effect via `POST /provider/reconcile`, and finalizes locally without issuing a second generation call.

### 4.4 Internal Persistence

Transactional SQLite in the `sidecar-ledger` volume. Two tables:

**Effects ledger** — committed, deduplicated outcomes. Enforces:
- `UNIQUE (namespace, idempotency_key)`
- same key + same fingerprint returns original effect
- same key + different fingerprint returns 409
- at most one successful effect for `(namespace, run_item, effect_kind, request_fingerprint)`

**Request attempts log** — append-only. Every inbound `POST /provider/generate` inserts a `request_kind = 'generate'` row before the idempotency check; every inbound `POST /provider/reconcile` inserts a `request_kind = 'reconcile'` row before lookup. Columns: `(namespace, idempotency_key_hash, request_kind CHECK (request_kind IN ('generate', 'reconcile')), request_fingerprint, sequence INTEGER PRIMARY KEY AUTOINCREMENT, received_at)`. The table is never pruned, updated, or used for idempotency. Tests filter by both key hash and kind so reconciliation evidence cannot inflate the generation-dispatch count.

The ledger and log are hidden behind the HTTP interface. Callers never access the SQLite file or volume directly.

### 4.5 Adapter Mode Boundary (ADR 0009)

The controlled-provider sidecar serves **Generation only** (`controlled_mock` mode). It exercises adapter error handling, SIGKILL recovery, and idempotency — none of which require decision-eligible evidence.

Experiments and Scale require **decision-eligible Meta evidence** (`customer_sandbox` mode per ADR 0009). This evidence is produced by the existing database-backed `FakeMetaAdapter`, not the HTTP sidecar. `controlled_mock` evidence is explicitly excluded from Winner decisions, ScaleAdvice, and any cross-mode references by two independent mechanisms per ADR 0009: RLS `USING/WITH CHECK` on `(workspace_id, meta_mode)` **and** composite foreign keys that include `(workspace_id, meta_mode, …)` to prevent cross-mode references at the schema level. RLS controls visibility; composite FKs prevent relational joins across modes.

Meta observation control belongs to `meta-scenario-fixtures.ts` and routes through the normal same-origin BFF boundary to the `FakeMetaAdapter` scenario interface, not through the Generation sidecar:

```typescript
advanceMetaObservation(session, namespace, {
  scenario: MetaScenarioId,
  experiment?: EntityRef<'experiment'>,
  enrollment?: EntityRef<'scale_enrollment'>,
  candidateRefs?: EntityRef<'asset_version'>[],
}): Promise<MetaObservationAdvancedResult>
```

This loads or advances curated `customer_sandbox` scenario data into the `FakeMetaAdapter`'s durable database tables. The real sync pipeline, projection, judgment, evidence snapshots, and advice generation then run through application and Worker paths. The FakeMetaAdapter's scenario data is pre-configured per test step (insufficient metrics, decisive winner, warming, declining) and produces evidence rows with immutable `customer_sandbox` provenance.

The sidecar's `advanceMetaObservationScenario` endpoint does not exist. Meta observation and Generation provider dispatch are distinct adapter modes with distinct persistence and distinct provenance.

## 5. Two-Phase Provisioning Module

### 5.1 Deep Module: `web/lib/control-plane-provisioning/`

`index.ts` is the sole public interface. `begin.ts`, `complete.ts`, and Prisma details remain internal.

**`beginProvisioning(input)`** → `BeginProvisioningResult`

Accepts a caller-supplied idempotency key and:
1. Idempotently ensures the versioned platform operator exists (lookup by stable identifier within the `golden-path` namespace; create only if absent). Production provisioning requires an explicitly configured operator identity.
2. Idempotently ensures an effective Beta `PlanVersion`. Requires exact match on `plan_key + semantic_version + entitlement_hash`. If an existing version has different entitlements, fails rather than silently reusing or replacing.
3. Creates a fresh `Team` in `PROVISIONING` state.
4. Creates a one-time `Invitation` through the existing invitation model and its production consumption path. No parallel `BetaAccessInvitation` model. Beta access is represented by the PlanVersion, AccessGrant, binding, and published entitlement snapshots.
5. Records progress in a durable control-plane provisioning journal: provisioning reference, Team/operator/PlanVersion/invited-email references, current phase, and bounded failure code.

A lost command response retried with the same idempotency key must not create a second Team, invitation, grant, or snapshot.

```typescript
interface BeginProvisioningResult {
  provisioningRef: string;
  workspaceRef: string;
  workspaceSlug: string;
  ownerInvitation: SecretInvitationCapability;
}
```

**`completeProvisioning(provisioningRef)`** → `CompleteProvisioningResult`

Does not accept `ownerUserId`. The module already knows the invited address and Team.

1. Resolves the consumed invitation and verifies exactly one matching Owner membership internally.
2. Calls the existing `provisionWorkspace(...)`.
3. Waits for provisioning outbox delivery and domain acknowledgment/projection version.
4. Waits for Spend Guardrail creation.
5. Atomically transitions Team to `ACTIVE` and enqueues the active snapshot in one serializable transaction (via `activateWorkspaceAndPublish(...)` or equivalent). A crash cannot leave an ACTIVE Team whose active snapshot was never published.
6. Waits for active outbox delivery and matching active domain projection.
7. Updates the provisioning journal.

```typescript
interface CompleteProvisioningResult {
  provisioningRef: string;
  workspaceRef: string;
  workspaceSlug: string;
  ownerMembershipRef: string;
  provisioningSnapshotVersion: number;
  activeSnapshotVersion: number;
}
```

The Workspace identity is the canonical Team/Workspace identity, not a separately generated domain UUID.

### 5.2 Command Adapters

**`web/scripts/provision-workspace.ts`** (production operator CLI):
Refactored to explicitly two-phase:
```
provision-workspace begin --config <path>
provision-workspace complete --provisioning-ref <ref>
```
If the invited Owner has not completed enrollment, `complete` exits with a bounded `owner_membership_pending` error. No synthetic enrollment step. No immediate-ACTIVE shortcut that bypasses the invitation ownership seam.

**`web/scripts/golden-path-provisioning.ts`** (E2E command adapter):
Thin adapter invoked by `provisioning.ts` fixture. Accepts subcommands `begin` and `complete`. Input via JSON on stdin. Results via JSON on stdout. Invitation capability via dedicated captured file descriptor (fd 3). Sanitized stderr containing only bounded error codes.

### 5.3 Command Transport

`provisioning.ts` uses asynchronous `execFile` with:
- No shell.
- Bounded stdout/stderr buffers.
- Explicit timeout.
- JSON input over stdin.
- Safe result over stdout.
- Invitation capability over dedicated captured file descriptor (fd 3).
- Sanitized stderr containing only bounded error codes.

Never includes raw child output in thrown errors. Never passes secrets as CLI arguments.

### 5.4 Secret Persistence Invariants

The invitation mechanism necessarily persists an invitation record in the control-plane database. The invariant is:

> The raw invitation capability traverses the E2E harness only in memory and never enters logs, reports, screenshots, traces, videos, command arguments, or uploaded artifacts.

After onboarding, authentication transfers through an in-memory `storageState` object wrapped in an opaque session capability — never written to disk. The authenticated context still requires SafeCapture masking.

## 6. Fixture Composition

### 6.1 Playwright Configuration

`playwright.config.golden.ts`:
```typescript
{
  globalTimeout: 40 * 60_000,
  timeout: 40 * 60_000,
  workers: 1,
  retries: 0,
  use: {
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
}
```

The complete CI job must remain below 45 minutes.

### 6.2 Fixture Modules

`fixtures/index.ts` exports a composed `test` extending `base.test` and `expect`. All other fixture modules are internal.

| Fixture key | Module | Responsibility |
|---|---|---|
| `provisioning` | `provisioning.ts` | Wraps the provisioning command adapter |
| `onboarding` | `onboarding-harness.ts` | Invitation → signup → MFA → recovery → opaque session |
| `workspaceSetup` | `workspace-setup.ts` | Authenticated Workspace settings UI actions, including Meta Sandbox connection creation |
| `serviceControl` | `service-control.ts` | Container lifecycle via lifecycle broker (no sidecar knowledge) |
| `providerControl` | `controlled-provider-fixtures.ts` | Generation sidecar barriers, checkpoints, effects, and request attempts |
| `metaScenarios` | `meta-scenario-fixtures.ts` | FakeMetaAdapter scenario control through the same-origin BFF |
| `evidence` | `evidence-inspector.ts` | Read-only persisted-state queries via `inspect_db.py` |
| `safety` | `runtime-safety.ts` | NetworkSpy, topology validation, no-egress probes, effective config |
| `artifacts` | `evidence-recorder.ts` | Cumulative evidence, timing, SafeCapture on failure, report finalization |
| `library` | `area-drivers/library.ts` | Library UI actions → `LibraryEvidence` |
| `generation` | `area-drivers/generation.ts` | Generation UI actions → `GenerationSubmittedEvidence`, `GenerationCompletedEvidence` |
| `assets` | `area-drivers/assets.ts` | Assets UI actions → `AssetsEvidence` |
| `experiments` | `area-drivers/experiments.ts` | Experiments UI actions → typed evidence per sub-step |
| `localization` | `area-drivers/localization.ts` | Localization UI actions → typed evidence per sub-step |
| `scale` | `area-drivers/scale.ts` | Scale UI actions → typed evidence per sub-step |

All stateful fixtures are test-scoped. Teardown is idempotent and ordered (authoritative sequence — Section 9.3 elaborates fixture steps 1–8; the CI workflow owns step 9):
1. Record terminal step result.
2. SafeCapture from the main context (while browser context is still open).
3. Close browser contexts.
4. Release sidecar barriers.
5. Capture sanitized sidecar evidence.
6. Redact logs.
7. Write reports.
8. Validate artifacts.
9. Tear down Compose.

### 6.3 Context Lifecycle

The default Playwright `page` fixture is not used. Fixtures depend on `browser`, create contexts manually, and install safety before creating any page:

```
browser.newContext({ storageState: undefined })
→ install NetworkSpy on BrowserContext
→ create Page
→ return context/page to caller
```

Area drivers accept `OwnerSessionCapability` or its safe `Page`; they never depend on the base `page` fixture. NetworkSpy is installed on every context (onboarding and main) before the first page is created.

### 6.4 NetworkSpy

Installed on `BrowserContext`, not `Page`. Must:
- Install before the first page.
- Allowlist only configured web and BlobStore origins.
- Inspect every method and resource type, not only mutations.
- Cover WebSockets.
- Abort and record all non-allowlisted requests.
- Use `serviceWorkers: 'block'`.
- Sanitize URL evidence (strip query params, credentials).
- Never scan arbitrary response bodies.

### 6.5 Onboarding Harness

Two interfaces:

```typescript
enrollOwner(invitation: SecretInvitationCapability): Promise<OwnerSessionCapability>
enrollMember(invitation: SecretInvitationCapability): Promise<MemberSessionCapability>
```

Internally creates several disposable secret-bearing contexts:
1. Signup and TOTP MFA enrollment.
2. Recovery-code single-use redemption.
3. Attempted recovery-code reuse (must fail).
4. Creation of the final authenticated context.

Each secret-bearing context has NetworkSpy installed and is closed after use. None are recorded.

`OwnerSessionCapability` does not expose the TOTP secret or raw storage state:
```typescript
interface OwnerSessionCapability {
  page(): Page;
  performStepUp(action: StepUpAction): Promise<OpaqueStepUpGrant>;
  createFreshContext(): Promise<OwnerSessionCapability>;  // from in-memory auth
  close(): Promise<void>;
}
```

`MemberSessionCapability` is similarly opaque but with a restricted interface (no step-up).

The TOTP secret remains in the onboarding module's private closure.

The context returned immediately after MFA enrollment does not automatically become the long-lived main context. After the Member flow completes in Step 0, a fresh main Owner context is created via `createFreshContext()` from the in-memory authentication capability.

### 6.5.1 Invitation Mail Sink

Source: `test-support/mail-sink/`

**Compose service**: `mail-sink` — a lightweight Node.js SMTP server that accepts mail on the internal network only. The Compose overlay sets the control-plane email transport to `smtp://mail-sink:2525` (no TLS, no authentication — internal-only network). Health check: `GET /health` returns 200.

**Capture protocol**: The mail sink exposes one authenticated test-control HTTP endpoint:
- `POST /api/messages/query` with `{ recipient, timeoutMs }` — long-polls until a matching message arrives or the timeout expires. Returns `{ to, invitationPath }` on success, 404 on timeout. The response contains only the parsed invitation URL path — never the full rendered email body, headers, or raw SMTP data. Recipient data stays out of request URLs and access logs.

**Access control**: Network reachability alone is not treated as authorization because every service shares the internal network. The runner generates a high-entropy per-run `MAIL_SINK_CONTROL_TOKEN` and mounts it as a read-only Docker secret into only `mail-sink` and `playwright`. The HTTP endpoint requires `Authorization: Bearer <token>`, compares it in constant time, and returns 401 without revealing message existence on failure. Web, API, Worker, and controlled-provider do not receive this capability. The token is never written to environment dumps, logs, reports, screenshots, or artifacts. The SMTP port remains unauthenticated but accepts connections only inside the internal network and exposes no read capability.

**Teardown**: Messages are held in memory only. The mail sink retains messages only for the duration of the Compose session; `docker compose down` removes it. No messages are written to disk, logs, volumes, or artifacts.

The onboarding harness captures the Member invitation capability from the mail sink:

```typescript
captureInvitation(mailSink, { recipientEmail, timeout }): Promise<SecretInvitationCapability>
```

The captured capability is held in memory only. It captures only the invitation URL path and validates the token format before returning the capability. The raw invitation URL never enters logs, reports, screenshots, or uploaded artifacts.

### 6.5.2 Workspace Setup

`workspace-setup.ts` performs authenticated Workspace configuration only through visible SaaS UI actions and same-origin responses:

```typescript
createMetaSandboxConnection(
  session: OwnerSessionCapability,
  input: { mode: 'customer_sandbox' },
): Promise<{
  mode: 'customer_sandbox';
  observedVia: 'dom' | 'same_origin_response';
}>
```

The UI action is available only after the Workspace is ACTIVE and requires Owner authorization. The BFF resolves the Workspace from the route and membership, while the domain service creates the immutable `(workspace_id, meta_mode)` connection. The fixture cannot pass a Workspace ID, call FastAPI directly, or change an existing connection's mode.

### 6.6 Service Control

Calls the lifecycle broker through narrow IPC. Uses a closed `RuntimeService` union:

```typescript
type RuntimeService = 'web' | 'api' | 'worker';
```

Methods:
- `stop(service: RuntimeService)`
- `start(service: RuntimeService)` — polls service-specific readiness
- `kill(service: 'worker', signal: 'SIGKILL')` — only Worker supports SIGKILL
- `restart(service: RuntimeService)`
- `status(service: RuntimeService)`
- `inspectNetwork(service: RuntimeService): Promise<NetworkAttestation>` — closed broker projection only

Container resolution via `docker compose ps -q <service>`. Project name and Compose paths validated once at initialization.

### 6.7 Adapter-Control Fixtures

The Generation sidecar and Meta scenario controls have different transports, provenance, and change reasons, so they are separate fixtures composed only by `fixtures/index.ts`.

**`providerControl`** — controlled-provider sidecar (Generation only, `controlled_mock` mode). Uses the sidecar's test-control HTTP interface through `fetch`:

```typescript
registerBarrier(namespace, { runRef, matchOrdinal }): Promise<BarrierRef>
awaitCheckpoint(namespace, barrierName, opts): Promise<void>  // delegates to wait_adapter_checkpoint.py
releaseBarrier(namespace, barrierName): Promise<void>
inspectEffects(namespace, { limit }): Promise<SanitizedEffectSummary[]>
inspectRequestAttempts(namespace, {
  idempotencyKeyHash: string,
  requestKind: 'generate' | 'reconcile',
  limit: number,
}): Promise<RequestAttemptSummary[]>
cleanup(namespace): Promise<void>
```

**`metaScenarios`** — FakeMetaAdapter scenario control (Experiments/Scale, `customer_sandbox` mode per ADR 0009). Calls a same-origin BFF test-support route with the authenticated Owner session. The browser never receives or derives a domain bearer token:

```typescript
advanceMetaObservation(session: OwnerSessionCapability, namespace: string, {
  scenario: MetaScenarioId,
  experiment?: EntityRef<'experiment'>,
  enrollment?: EntityRef<'scale_enrollment'>,
  candidateRefs?: EntityRef<'asset_version'>[],
}): Promise<MetaObservationAdvancedResult>
```

`MetaScenarioId` is a closed union: `'insufficient_metrics' | 'decisive_winner' | 'scale_warming' | 'scale_declining'`.

The browser-facing endpoint is `POST /api/domain/v1/w/{slug}/test-support/meta-observation/advance`. It exists only in the E2E build with `META_ADAPTER=fake_meta`. The same-origin BFF resolves `{slug}`, verifies active Owner membership, mints the normal per-request delegated-user token, and forwards to the private FastAPI test-support operation. FastAPI derives `workspace_id` from verified claims; neither the request body nor the fixture may select a Workspace. It verifies every referenced experiment, enrollment, and candidate belongs to that Workspace and its `customer_sandbox` Meta connection before loading curated data into the `(workspace_id, meta_mode)` partition. It returns `{ scenarioApplied, observationCount, provenance }`. Both BFF and FastAPI routes are absent from production builds.

`awaitCheckpoint` uses `wait_adapter_checkpoint.py` as the single checkpoint observer and validates its output against a closed TypeScript schema. No second polling implementation.

`cleanup` releases blocked barriers but does not erase the effect ledger before artifacts are serialized. Required teardown order:
1. Release blocked barriers.
2. Capture sanitized effect evidence.
3. Write and validate reports.
4. Purge namespace or remove volume.

`inspectEffects` exposes only idempotency-key hashes and opaque effect references, not raw keys.

### 6.8 Evidence Inspector

Read-only persisted-state queries via `inspect_db.py`. Every Python result is validated against a closed schema after execution.

Methods:
- `assertZeroBusinessEntities(workspaceRef)` — covers: Product, Persona, published template, PromptPolicy, DataSource, GenerationRun, AssetVersion, Experiment, LocalizationBatch, ScaleEnrollment, ScaleAdvice.
- `queryCallEvidence(workspaceRef, filters)` — sanitized call-intent records.
- `queryProviderCostLedger(workspaceRef)` — cost consistency: reservation/settlement/release counts and classification.
- `captureCanonicalSnapshot(workspaceRef)` — allowlisted projection only: opaque entity refs, lifecycle states, revision numbers, version/content hashes, lineage refs, bounded cost totals. Excludes: timestamps, lease owners/heartbeats, process/container IDs, presentation ordering, exact financial amounts, mutable sidecar state. Uses cost consistency classifications and bounded counts rather than exact amounts.
- `compareSnapshots(before, after)` — exact match on accepted stable fields.
- `assertNoWinnerDecision(workspaceRef, experimentRef)` — confirms no Winner decision exists for the given experiment.
- `assertZeroMetaWrites(workspaceRef)` — distinguishes simulated vs real write intents.
- `assertWorkspaceMetaProvenance(workspaceRef, expectedMode)` — verifies the Workspace's persisted Meta connection carries the expected immutable provenance.
- `assertNonVacuousMetaEvidence(workspaceRef, { expectedProvenance, expectedScenarios, frozenCandidateCount })` — verifies: (1) total intent count is positive and derived from the frozen candidate set (not zero-by-absence), (2) all intents carry the expected provenance (`customer_sandbox`), (3) exactly the expected observation scenarios were applied, (4) zero credential use records, (5) zero HTTP-adapter attempts. Fails if any count is zero when it should be positive (vacuous pass) or positive when it should be zero (leaked real call).
- `assertNoRunnableOrInFlightJobs(workspaceRef)` — quiescence check.
- `assertOperationsSettled(workspaceRef)` — all operations in terminal state.
- `readJobLease(runRef)` — read-only job lease state for SIGKILL recovery observation. Never claims, heartbeats, updates, or requeues.
- `assertCallEvidenceModeConsistency(workspaceRef)` — all evidence in correct mode.

Entity IDs may come only from visible UI state or same-origin responses caused by the current UI action. Evidence inspector is assertion-only — drivers cannot obtain missing IDs from it.

### 6.9 Runtime Safety

- `validateTopologyAttestation()` — reads the host-side topology attestation from the shared volume (Section 3.5 Pass 3). Validates freshness, completeness, and all invariants passed. Aborts the test if missing, stale, or failed.
- `validateRestartedServiceNetwork(service, attestation)` — validates the broker's closed `inspect_network` result after each restart; never accepts raw Docker output.
- `probeNoEgress(project)` — multi-service, multi-protocol egress probes (Section 3.6). The Playwright-local probe runs directly; other service probes use the broker's closed `probe_egress` operation, so Playwright still receives no Docker access.
- `probeEffectiveConfig(project)` — boolean-only output per service:
  ```json
  {
    "generation_adapter": "controlled_mock",
    "meta_adapter": "fake_meta",
    "meta_write_enabled": false,
    "meta_write_token_present": false,
    "meta_write_ready": false
  }
  ```
  Probes API and Worker independently after initial startup and after every relevant restart. Never emits token values. Includes `meta_adapter` to attest the FakeMetaAdapter is active.
- `installNetworkSpy(context)` — context-level spy (Section 6.4).
- `assertNetworkSpyClean()` — final assertion.

### 6.10 Artifacts Fixture

Owns cumulative evidence collection and always-produced reports:
- Records `AreaEvidence`, sanitized sidecar evidence, assertion outcomes, and timings.
- Converts runtime IDs to opaque artifact aliases using an in-memory, run-scoped HMAC key (never persisted): `resource_ref = HMAC(runKey, kind || rawId)`. Only the opaque reference, kind, and area enter artifacts.
- Invokes SafeCapture on failure (optional, not on every step transition).
- Writes and validates final reports in fixture teardown regardless of pass or failure.
- Initializes a valid `interrupted` report before Compose starts; teardown replaces it when possible.
- `markScenarioComplete()` may be called from the last explicit test step; teardown owns finalization.

## 7. Area Drivers and Evidence Types

### 7.1 Evidence Type System

```typescript
type Area = 'library' | 'generation' | 'assets' | 'experiments' | 'localization' | 'scale';

type EntityKind =
  | 'product' | 'product_version' | 'offer_version'
  | 'persona_version' | 'template_version'
  | 'prompt_policy_version' | 'data_source_version'
  | 'generation_run' | 'asset_version'
  | 'experiment' | 'winner_decision'
  | 'localization_batch' | 'scale_enrollment'
  | 'advice_thread' | 'generation_draft'
  | 'campaign' | 'ad_set' | 'ad';

interface EntityRef<K extends EntityKind> {
  readonly kind: K;
  readonly runtimeId: BrandedOpaqueId<K>;
  readonly observedVia: 'dom' | 'same_origin_response';
}

interface EvidenceEnvelope<A extends Area, C extends string> {
  readonly schemaVersion: 1;
  readonly area: A;
  readonly claims: readonly {
    code: C;
    status: 'passed';
  }[];
}
```

An entity reference may come only from visible UI state or the same-origin response caused by the current UI action. Drivers cannot obtain missing IDs from `evidence-inspector`.

### 7.2 Area-Specific Evidence Types

```typescript
interface LibraryEvidence extends EvidenceEnvelope<'library', LibraryClaimCode> {
  product: EntityRef<'product'>;
  productVersion: EntityRef<'product_version'>;
  offerVersion: EntityRef<'offer_version'>;
  personaVersion: EntityRef<'persona_version'>;
  templateVersion: EntityRef<'template_version'>;
  promptPolicyVersion: EntityRef<'prompt_policy_version'>;
  dataSourceVersion: EntityRef<'data_source_version'>;
}

interface GenerationSubmittedEvidence extends EvidenceEnvelope<'generation', GenerationClaimCode> {
  run: EntityRef<'generation_run'>;
  expectedItemCount: number;
}

interface GenerationCompletedEvidence extends EvidenceEnvelope<'generation', GenerationClaimCode> {
  run: EntityRef<'generation_run'>;
  assetVersions: EntityRef<'asset_version'>[];
}

interface AssetsEvidence extends EvidenceEnvelope<'assets', AssetsClaimCode> {
  approvedRefs: EntityRef<'asset_version'>[];
  reApprovedRef: EntityRef<'asset_version'>;
  originalNeedsChangesRef: EntityRef<'asset_version'>;
  exportClaims: readonly { code: string; status: 'passed' }[];
}

interface ExperimentFrozenEvidence extends EvidenceEnvelope<'experiments', ExperimentClaimCode> {
  experiment: EntityRef<'experiment'>;
  candidateRefs: EntityRef<'asset_version'>[];
}

interface ExperimentLaunchedEvidence extends EvidenceEnvelope<'experiments', ExperimentClaimCode> {
  experiment: EntityRef<'experiment'>;
  campaignRef: EntityRef<'campaign'>;
  adSetRefs: EntityRef<'ad_set'>[];
  adRefs: EntityRef<'ad'>[];
  candidateDeliveries: readonly {
    candidateRef: EntityRef<'asset_version'>;
    adSetRef: EntityRef<'ad_set'>;
    adRef: EntityRef<'ad'>;
  }[];
}

interface ExperimentInconclusiveEvidence extends EvidenceEnvelope<'experiments', ExperimentClaimCode> {
  experiment: EntityRef<'experiment'>;
}

interface ExperimentWinnerEvidence extends EvidenceEnvelope<'experiments', ExperimentClaimCode> {
  experiment: EntityRef<'experiment'>;
  winnerAssetVersion: EntityRef<'asset_version'>;
  winnerDecision: EntityRef<'winner_decision'>;
}

interface LocalizationBatchEvidence extends EvidenceEnvelope<'localization', LocalizationClaimCode> {
  batch: EntityRef<'localization_batch'>;
  itemOutcomes: Record<string, { status: string; localizedAssetVersion?: EntityRef<'asset_version'> }>;
}

interface LocalizationOutcomesEvidence extends EvidenceEnvelope<'localization', LocalizationClaimCode> {
  batch: EntityRef<'localization_batch'>;
  retriedMarket: string;
  retriedAttemptCount: number;
  localizedAssetVersions: EntityRef<'asset_version'>[];
}

interface ScaleEnrollmentEvidence extends EvidenceEnvelope<'scale', ScaleClaimCode> {
  enrollment: EntityRef<'scale_enrollment'>;
}

interface ScaleEvidence extends EvidenceEnvelope<'scale', ScaleClaimCode> {
  enrollment: EntityRef<'scale_enrollment'>;
  adviceThread: EntityRef<'advice_thread'>;
  generationDraft: EntityRef<'generation_draft'>;
}
```

Raw runtime IDs stay in memory. The `artifacts` fixture maps them to bounded artifact aliases before serialization.

### 7.3 Driver Constraints

- Area drivers accept `OwnerSessionCapability` and narrow typed inputs from preceding areas — not entire prior evidence objects.
- Drivers may assert UI-visible outcomes but never query the database.
- Drivers return typed `AreaEvidence`; they never write artifacts directly.
- Drivers do not return "adapter action needed" markers or accept callbacks. The spec knows the sequence.
- Each driver provides a read-only `verifyPersistedState(session, evidence)` method for post-restart verification.

### 7.4 Driver Interfaces

**Library**: `setup(session, config)` → `LibraryEvidence`

**Generation**:
- `submit(session, { productVersion, templateVersion, policyVersion, dataSourceVersion })` → `GenerationSubmittedEvidence`
- `observeCompletion(session, { run, expectedItemCount })` → `GenerationCompletedEvidence`

**Assets**: `reviewAndExport(session, { approvedAssets, needsChangesAsset, rejectedAsset, skipAsset })` → `AssetsEvidence`
ZIP manifest inspection returns only closed claims and references — no manifest content, filenames, prompts, or archive contents.

**Experiments**:
- `createAndFreeze(session, { approvedAssets, reApprovedAsset, offerVersion })` → `ExperimentFrozenEvidence`
- `launchPaused(session, { experiment })` → `ExperimentLaunchedEvidence` (ends after PAUSED saga; returns delivery references for Scale)
- `recordInconclusive(session, { experiment })` → `ExperimentInconclusiveEvidence` (from insufficient persisted evidence snapshot)
- `confirmWinner(session, { experiment })` → `ExperimentWinnerEvidence`

**Localization**:
- `createAndConfirmBatch(session, { winnerAssetVersion, winnerDecision, offerVersion, targetMarkets })` → `LocalizationBatchEvidence`
- `observeOutcomesAndRetry(session, { batch, expectedOutcomes, retryMarket, retryExpectedOutcome })` → `LocalizationOutcomesEvidence`

Localization adapter scenarios are configured before batch submission. Expected outcomes are exact:
- DE → `succeeded`
- CA → `skipped_no_change_required`
- JP → `failed` → explicit UI retry → `succeeded` (creates new paid intent/attempt)

Successful localized AssetVersions return to `pending` human quality and do not inherit Winner status.

**Scale**:
- `createAndActivateEnrollment(session, { winnerDecision, winnerAssetVersion, offerVersion, market, placement, landingPage, campaign, adSet, ad })` → `ScaleEnrollmentEvidence`
- `observeAdviceAndConvertDraft(session, { enrollment })` → `ScaleEvidence`

The driver may assert the GenerationDraft remains unsubmitted. "No cost" and "no Meta mutation" must be proven by evidence counts before and after conversion, not by UI assertions.

## 8. Golden-Path Test Flow (Steps 0–10)

### Step 0 — Safety, Provisioning, Owner, Member, and Emptiness

**0.1**: Validate topology attestation (host-side Passes 1–2 already completed; Playwright reads and validates the attestation per Section 3.5 Pass 3), probe no-egress, probe effective config (including `meta_adapter`), and record config attestation. No Workspace-scoped assertion runs yet because the Workspace and its Meta connection do not exist.

**0.2**: `beginProvisioning(input)` with idempotency key.

**0.3**: `enrollOwner(invitation)` — fresh browser, signup, MFA, recovery codes, secret contexts closed.

**0.4**: Create Member invitation through the Owner UI. The invitation email is captured by the in-memory mail sink (Section 6.5.1). `captureInvitation(mailSink, { recipientEmail, timeout })` extracts the `SecretInvitationCapability` from the captured message without writing the invitation URL to disk, logs, or artifacts.

**0.5**: `enrollMember(memberInvitation)` — disposable unrecorded context. Verify the Member sees exactly this Workspace. Verify Owner-only settings are forbidden. Close the Member context.

**0.6**: Close the temporary Owner context.

**0.7**: Create the fresh main Owner context from the in-memory authentication capability. This becomes the long-lived session for Steps 1–8.

**0.8**: `completeProvisioning(provisioningRef)` and wait for the ACTIVE domain projection.

**0.9**: Through the normal Workspace settings UI and same-origin BFF, `workspaceSetup.createMetaSandboxConnection(session, { mode: 'customer_sandbox' })`. This creates the Workspace-bound connection; no fixture, environment variable, or request body selects provenance behind the UI.

**0.10**: `evidence.assertWorkspaceMetaProvenance(workspaceRef, 'customer_sandbox')`.

**0.11**: `evidence.assertZeroBusinessEntities(workspaceRef)`. The Meta connection is Workspace configuration, not a seeded business entity.

### Step 1 — Creative Library

`const libraryEv = await library.setup(session, config)` → `LibraryEvidence`

Creates: Product with reference image, Offer, Persona, Dimension Template (applied), Prompt Policy, manual Data Source with multi-market pricing. All entities created through UI. Evidence inspector verifies persisted state.

### Step 2 — Resolution Preview and Generation Submission

**2.1**: `serviceControl.stop('worker')`.

**2.2**: `const submitted = await generation.submit(session, { productVersion, templateVersion, policyVersion, dataSourceVersion })` → `GenerationSubmittedEvidence`.

### Step 3 — Deterministic Worker Interruption and Recovery

**3.1**: `providerControl.registerBarrier(namespace, { runRef: submitted.run, matchOrdinal: 0 })`.

**3.2**: `serviceControl.start('worker')`.

**3.3**: `providerControl.awaitCheckpoint(namespace, barrierName, { expected: terminalCheckpointState })`.

**3.4**: `serviceControl.kill('worker', 'SIGKILL')`.

**3.5**: `evidence.readJobLease(submitted.run)` — verify state was `running`, lease owner/deadline persisted.

**3.6**: Poll read-only until the observed lease deadline has passed. Do not restart Worker immediately.

**3.7**: `serviceControl.start('worker')`. Poll persisted recovery classification.

**3.8**: `const completed = await generation.observeCompletion(session, { run: submitted.run, expectedItemCount: submitted.expectedItemCount })` → `GenerationCompletedEvidence`.

**3.9**: Explicit recovery assertions:
- The targeted attempt was committed before dispatch.
- The Job became uncertain or entered the expected recovery classification.
- Reconciliation looked up the original idempotency key.
- The sidecar effects ledger recorded one successful effect for the targeted RunItem's idempotency key.
- **No second dispatch**: `providerControl.inspectRequestAttempts(namespace, { idempotencyKeyHash, requestKind: 'generate', limit })` returns exactly one row for the targeted key. A second dispatch would appear even though the effects ledger deduplicates it.
- **Reconciliation occurred**: the same query with `requestKind: 'reconcile'` returns exactly one row for the targeted key. Generate and reconcile counts are never combined.
- Every other incomplete safe item resumed.
- Each reservation was settled or released exactly once.
- One final AssetVersion exists per successful item.

**3.10**: Sidecar effects inspection with pagination bounded by expected item count; fail if results are truncated. Request-attempt inspection filters by both idempotency-key hash and request kind, proving exactly one `generate` and exactly one `reconcile` for the SIGKILL-targeted item.

**3.11**: Re-probe effective config after Worker restart.

### Step 4 — Assets Review, Regeneration, and Export

`const assetsEv = await assets.reviewAndExport(session, { approvedAssets, needsChangesAsset, rejectedAsset, skipAsset })` → `AssetsEvidence`.

**4.1**: Keyboard review (A/C/X/space/left) — approve, mark Needs Changes, reject, skip.

**4.2**: Re-generate the Needs Changes asset. A new AssetVersion is created; the old version is preserved (immutable history).

**4.3**: **Re-approve the regenerated asset through the UI**. The regenerated AssetVersion starts in an unapproved state. The Owner reviews it again via the keyboard flow and approves it. Only after this explicit re-approval is the asset eligible as an Experiment candidate. Evidence records both the original `needsChanges` decision and the subsequent `approved` decision on the new version.

**4.4**: ZIP export with bounded manifest claims.

### Step 5 — Experiment Freeze and Simulated PAUSED Launch

**5.1**: `const frozen = await experiments.createAndFreeze(session, { approvedAssets: assetsEv.approvedRefs, reApprovedAsset: assetsEv.reApprovedRef, offerVersion })` → `ExperimentFrozenEvidence`. The `reApprovedAsset` is the regenerated asset from Step 4.3, which has an explicit approval decision on its current version. Subsequent steps use `frozen.experiment` and `frozen.candidateRefs`; no implicit alias is used.

**5.2**: Preview → Owner step-up → second confirmation → PAUSED Campaign/AdSet/Ad projections.

`const launched = await experiments.launchPaused(session, { experiment: frozen.experiment })` → `ExperimentLaunchedEvidence` with exact simulated delivery references (campaign, adSet, ad) required by Scale.

### Step 6 — Metrics, Judgment, and Confirmed Winner

All Meta observation evidence uses `customer_sandbox` provenance via the FakeMetaAdapter (ADR 0009). This mode is decision-eligible; `controlled_mock` evidence from the Generation sidecar is explicitly excluded from judgment.

**6.1**: Load insufficient-metrics `customer_sandbox` scenario → real sync/projection → UI records inconclusive judgment. Evidence proves no Winner exists. The first judgment comes from an insufficient persisted evidence snapshot, not merely absence of metrics.

```
metaScenarios.advanceMetaObservation(session, namespace, {
  scenario: 'insufficient_metrics',
  experiment: frozen.experiment,
})
// Wait for real sync/projection through FakeMetaAdapter → domain pipeline
experiments.recordInconclusive(session, { experiment: frozen.experiment })
evidence.assertNoWinnerDecision(workspaceRef, frozen.experiment)
```

**6.2**: Load decisive-metrics `customer_sandbox` scenario → real sync/projection/judgment → UI confirms Winner.

```
metaScenarios.advanceMetaObservation(session, namespace, {
  scenario: 'decisive_winner',
  experiment: frozen.experiment,
  candidateRefs: frozen.candidateRefs,
})
// Wait for real sync/projection/judgment pipeline with customer_sandbox provenance
const winner = await experiments.confirmWinner(session, { experiment: frozen.experiment })
```

### Step 7 — Localization Truth and Item Outcomes

**7.1**: Configure localization adapter scenarios before batch submission.

**7.2**: `const localizationBatch = await localization.createAndConfirmBatch(session, { winnerAssetVersion: winner.winnerAssetVersion, winnerDecision: winner.winnerDecision, offerVersion, targetMarkets: { DE: 'succeeded', CA: 'skipped_no_change_required', JP: 'failed' } })`.

**7.3**: Controlled scenarios execute through real Worker paths.

**7.4**: `const localizationEv = await localization.observeOutcomesAndRetry(session, { batch: localizationBatch.batch, expectedOutcomes, retryMarket: 'JP', retryExpectedOutcome: 'succeeded' })`.

JP retry creates a new paid intent/attempt. Successful localized AssetVersions return to `pending` human quality. Evidence inspector verifies call evidence.

### Step 8 — Scale Enrollment, Evidence, and Advice

**8.1**: Resolve exactly one `winnerDelivery` from `launched.candidateDeliveries` where `candidateRef === winner.winnerAssetVersion`; fail on zero or multiple matches. Then `const enrollmentEv = await scale.createAndActivateEnrollment(session, { winnerDecision: winner.winnerDecision, winnerAssetVersion: winner.winnerAssetVersion, offerVersion, market, placement, landingPage, campaign: launched.campaignRef, adSet: winnerDelivery.adSetRef, ad: winnerDelivery.adRef })`. Set `const enrollment = enrollmentEv.enrollment` for subsequent steps.

**8.2**: Load warming `customer_sandbox` scenario. Wait for persisted warming snapshot to become mature.

```
metaScenarios.advanceMetaObservation(session, namespace, {
  scenario: 'scale_warming',
  enrollment,
})
// Wait for real Worker to produce persisted evidence snapshots with customer_sandbox provenance
```

**8.3**: Load declining `customer_sandbox` scenario. Wait for AdviceThread to appear.

```
metaScenarios.advanceMetaObservation(session, namespace, {
  scenario: 'scale_declining',
  enrollment,
})
```

**8.4**: `const scaleEv = await scale.observeAdviceAndConvertDraft(session, { enrollment })` → `ScaleEvidence`.

**8.5**: Prove evaluated ScaleEvidenceSnapshot and versioned ScaleAdvice exist. Prove GenerationDraft is unsubmitted. Prove "no cost" and "no Meta mutation" by evidence counts before and after conversion.

**8.6**: Assert zero Meta writes via all three layers.

### Step 9 — Pre-Restart Canonical Snapshot

**9.1**: Quiescence assertions:
```
evidence.assertNoRunnableOrInFlightJobs(workspaceRef)
evidence.assertOperationsSettled(workspaceRef)
```

**9.2**: `evidence.captureCanonicalSnapshot(workspaceRef)` — allowlisted projection only.

### Step 10 — Restart and Final Verification

**10.1**: Restart Web, API, Worker. Wait for each service-specific readiness condition.

**10.2**: After each restart, call `serviceControl.inspectNetwork(service)` and pass the closed result to `safety.validateRestartedServiceNetwork(service, attestation)`. Fail before navigation if the replacement container is missing, dual-homed, attached to the wrong network/hash, or exposes a published port.

**10.3**: Re-probe effective API/Worker configuration after each relevant restart.

**10.4**: Create a fresh Owner login context (from in-memory auth capability).

**10.5**: Navigate all six areas with typed read-only verification:
```
library.verifyPersistedState(session, libraryEv)
generation.verifyPersistedState(session, completed)
assets.verifyPersistedState(session, assetsEv)
experiments.verifyPersistedState(session, winner)
localization.verifyPersistedState(session, localizationEv)
scale.verifyPersistedState(session, scaleEv)
```

**10.6**: Wait for quiescence again.

**10.7**: Capture post-restart canonical projection.

**10.8**: Compare exactly with Step 9 snapshot.

**10.9**: Final three-layer assertions:
- Browser NetworkSpy clean.
- Zero real Meta writes (distinguished from simulated intents).
- **Non-vacuous Meta evidence**: `evidence.assertNonVacuousMetaEvidence(workspaceRef, { expectedProvenance: 'customer_sandbox', expectedScenarios: ['insufficient_metrics', 'decisive_winner', 'scale_warming', 'scale_declining'], frozenCandidateCount: frozen.candidateRefs.length })`.
- Workspace Meta provenance: `evidence.assertWorkspaceMetaProvenance(workspaceRef, 'customer_sandbox')`.
- Call evidence mode consistency.
- Cost consistency.
- Lineage integrity.
- Provider effect evidence (zero duplicate successful effects).

**10.10**: `artifacts.markScenarioComplete()`. Fixture teardown finalizes reports.

## 9. Evidence Pipeline

### 9.1 SafeCapture (`web/e2e/golden-path/artifacts/safe-capture.ts`)

Uses positive allowlisting. Both conditions must be met for capture:
1. A registered checkpoint code identifying the capture point.
2. A matching allowlisted route classification for the current page.

If either is unknown, capture is refused with a bounded error code. Screenshots are optional and normally failure-only — not automatically taken at every step transition.

Uses Playwright's screenshot masking and an injected capture-only stylesheet rather than mutating application elements. If DOM mutation is unavoidable, restore in `finally`.

Enforces:
- Viewport-only capture.
- Animations and caret disabled.
- Bounded dimensions and bytes.
- No capture from secret-bearing contexts.
- No entity IDs or user data in filenames.
- EXIF/GPS stripped.
- Output to allowlisted artifact directory only.

### 9.2 Report Schemas

Canonical JSON Schemas in `contracts/e2e/`:

**`golden-path-report.schema.json`**:
```json
{
  "schema_version": "1.0",
  "run_id": "string",
  "commit_sha": "string",
  "started_at": "ISO8601",
  "completed_at": "ISO8601",
  "completion_status": "passed | failed | interrupted",
  "last_completed_step": "number",
  "failure_code": "string | null",
  "steps": [{
    "step": "number",
    "label": "string",
    "outcome": "passed | failed | skipped",
    "duration_ms": "number",
    "failure_code": "string | null",
    "assertions": [{
      "code": "AssertionCode",
      "passed": "boolean",
      "observed": "boolean | number | BoundedOutcome",
      "expected": "boolean | number | BoundedOutcome"
    }]
  }],
  "config_attestations": [{
    "service": "string",
    "captured_after_step": "number",
    "generation_adapter": "string",
    "meta_adapter": "string",
    "meta_write_enabled": "boolean",
    "meta_write_token_present": "boolean",
    "meta_write_ready": "boolean"
  }]
}
```

**`golden-path-evidence.schema.json`**:
```json
{
  "schema_version": "1.0",
  "run_id": "string",
  "entity_aliases": { "alias": { "kind": "EntityKind", "area": "Area" } },
  "provider_effects": {
    "total_count": "number",
    "unique_idempotency_key_hashes": ["string"],
    "duplicate_successful_effects": 0
  },
  "provider_request_attempts": {
    "target_idempotency_key_hash": "string",
    "generate_count": 1,
    "reconcile_count": 1,
    "results_truncated": false
  },
  "cost_consistency": {
    "reservation_count": "number",
    "settlement_count": "number",
    "release_count": "number",
    "classification": "consistent | inconsistent"
  },
  "meta_call_evidence": {
    "provenance": "customer_sandbox",
    "total_intents": "number (exact positive count derived from frozen candidate set)",
    "real_write_intents": 0,
    "simulated_write_intents": "number (must equal total_intents)",
    "campaign_intents": "number (exactly 1 per frozen experiment)",
    "adset_intents": "number (must match frozen candidate count)",
    "ad_intents": "number (must match frozen candidate count)",
    "observation_scenarios_applied": ["insufficient_metrics", "decisive_winner", "scale_warming", "scale_declining"],
    "credential_use_records": 0,
    "http_adapter_attempts": 0,
    "non_vacuous": true
  },
  "canonical_snapshot_match": "boolean"
}
```

TypeScript types in `schemas.ts` consume these canonical JSON Schemas. Python verifier also consumes them.

### 9.3 Evidence Recorder Initialization

A valid `interrupted` report is initialized before Compose starts. It contains the run ID, commit SHA, `completion_status: 'interrupted'`, `last_completed_step: -1`, and empty step/attestation arrays. Fixture teardown replaces this with the final report when possible. If the test crashes before teardown, the interrupted report survives as the artifact.

Artifact finalization in fixture teardown (not in a test step). **SafeCapture must run while the browser context is still open** — it captures from the live page. The ordering is:
1. Record terminal step result.
2. **SafeCapture from the main context** (failure-only screenshots, positive-allowlist checkpoint codes, viewport-only, masking applied). This MUST happen before `OwnerSessionCapability.close()`.
3. Close the main Owner context (`OwnerSessionCapability.close()`).
4. Release any blocked controlled-provider barriers. This is idempotent and does not erase the append-only ledger.
5. Capture sanitized sidecar evidence (sidecar is still running; only its namespaced, bounded HTTP test-control interface is used).
6. Redact bounded logs.
7. Write reports (validate JSON in memory, write to temp file, validate serialized bytes, atomic rename).
8. Validate artifacts.

Only after steps 1–8 complete does the CI workflow tear down Compose. This is the same authoritative order as Section 6.2.

### 9.4 Opaque Aliases

Runtime IDs are converted to opaque artifact aliases using an in-memory, run-scoped HMAC key:

```
resource_ref = HMAC(runKey, kind || rawId)
```

The run key is generated at test start and never persisted. Only the opaque reference, kind, and area enter artifacts. A plain deterministic hash cannot be used because runtime IDs could be guessed.

## 10. CI Workflows

### 10.1 PR Validation (`golden-path-pr.yml`)

**Trigger**: `pull_request` on paths affecting: E2E infrastructure, area drivers, fixtures, golden-path spec, Compose overlay, sidecar, provisioning module, worker coordinator, runtime, contracts, migrations, deployment, CI scripts, workflows, dependency manifests, governing design/ADR files.

**Permissions**: `contents: read`.

```yaml
jobs:
  golden-path-pr:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - checkout
      - generate per-run topology Ed25519 keypair and mail-sink control token
      - start lifecycle broker
      - build Compose images (cached)
      - scripts/ci/validate_topology.py --phase config  # host-side Pass 1 only; no final attestation
      - id: compose-start
        record: monotonic/UTC Compose start timestamp
      - docker compose -p gp-${{ github.run_id }}-${{ github.run_attempt }}
          -f docker-compose.yml -f docker-compose.golden-path.yml
          up -d postgres init-domain init-control web api worker controlled-provider mail-sink
      - wait for declared service health/readiness
      - scripts/ci/validate_topology.py --phase live
          --compose-started-at ${{ steps.compose-start.outputs.timestamp }}
          --write-signed-attestation  # Pass 2, after containers exist; exits nonzero on any invariant failure
      - docker compose ... run --rm playwright  # Playwright starts here only, inside Compose
      - if: always()
        run: scripts/ci/redact_compose_logs.py  # collect only web/api/worker from run start
      - id: verify
        if: always()
        run: scripts/ci/verify_golden_artifacts.py
      - if: always() && steps.verify.outcome == 'success'
        upload exact approved files
      - if: always() && steps.verify.outcome == 'failure'
        upload only constant validation-failure marker (verifier failure means artifacts may contain secrets)
      - if: always() && steps.verify.conclusion == 'skipped'
        upload only constant validation-failure marker (test crashed before artifacts existed)
      - if: always()
        run: |
          docker compose -p ... down -v --remove-orphans
          stop lifecycle broker and delete its UDS
          delete topology attestation, topology keypair, and mail-sink control-token file
```

Artifact retention: 7 days. No AcceptanceManifest and no production, customer, or provider secrets are available. The runner-generated topology signing material and mail-sink control capability are scoped to this Compose run, never uploaded, and deleted during `if: always()` teardown. `retries: 0`.

### 10.2 Formal Acceptance (`golden-path-acceptance.yml`)

**Trigger**: `workflow_dispatch` with required inputs: release commit SHA, optional milestone reference.

**Structure**: DAG of jobs, not a single serial job.

```
resolve-inputs        → validate immutable release inputs, build/resolve images once
                        with pinned OCI digests
     │
validate-contracts    → validate all suite-contract.ts files, confirm declared spec
                        and seed paths exist
     │
┌────┼────┐
│    │    │
area-library    area-generation    area-assets    area-experiments    area-localization    area-scale
     │
golden-path     → runs against the same image digests
     │
assemble        → assemble and validate AcceptanceManifest
     │
publish         → protected environment, minimal permissions (issues: write)
                  does not execute candidate repository scripts
                  publishes only the already-validated manifest
```

Each area suite and golden path consume the exact OCI digests built in `resolve-inputs`. `--no-cache` alone does not make a build reproducible.

The formal `golden-path` job uses the same Pass-1 → Compose-up/readiness → Pass-2/final-attestation ordering and the same `if: always()` secret cleanup as the PR workflow. It may not collapse both topology passes into a pre-start step.

Per-run storage-state deletion enforced. The publication job:
- Runs only after all required tests and validations pass.
- Uses the `acceptance` protected environment (requires approval).
- Has minimal permissions: `issues: write` for milestone cross-linking.
- Does not execute candidate repository scripts.
- Publishes only the already-validated manifest.

Artifact retention: 90 days.

### 10.3 Log Redaction

`scripts/ci/redact_compose_logs.py`:
- Collects only `web`, `api`, `worker` logs from the recorded run start.
- Pattern-based redaction for tokens, secrets, passwords, API keys, invitation URLs, TOTP secrets, recovery codes. Replaces with `[REDACTED:<category>]`.
- Tested against: plain, URL-encoded, JSON-escaped, multiline, and split-field secret formats.
- Bounds: maximum line length, maximum lines per service, maximum total sanitized bytes.
- On redaction failure: delete/quarantine the candidate output, emit only a constant `redaction_failed` marker.
- Atomic rename to final path.
- Second-pass validation scan.
- Raw logs never written to artifact directory or echoed to Actions console.
- CI must never enable shell tracing.

### 10.4 Artifact Verification

`scripts/ci/verify_golden_artifacts.py`:
- **Allowlist check**: Every file must match a pattern in the closed allowlist.
- **Schema validation**: JSON artifacts validated against canonical schemas from `contracts/e2e/`.
- **Secret scan**: All text files scanned for credential patterns.
- **Size bounds**: Per-file and aggregate maximums.
- **Completeness check**: All required artifacts present.
- **Structural checks**: Reject symlinks, hidden files, path traversal, unknown MIME/extensions, forbidden JSON keys, oversized individual lines/files, directories where regular files expected.

CI uploads only exact approved files when verification succeeds. On any verification failure or skip, uploads only a constant validation-failure marker — never the artifacts that failed verification, since they may contain secrets. All upload conditions use `always() && steps.verify.*` to ensure prior step failure does not suppress the upload.

## 11. AcceptanceManifest

Published only by the formal acceptance workflow's protected publication job. Every formal run produces a manifest; only a validated `passed` manifest counts as sign-off.

```typescript
interface AcceptanceManifest {
  schema_version: '1.0';
  outcome: 'passed' | 'failed';
  timestamp: string;
  commit_sha: string;
  git_ref: string;
  image_digests: Record<ServiceCode, string>;
  compose_config_hash: string;
  migration_heads: {
    control: string;
    domain: string;
  };
  evidence_file_hashes: {
    report: string;
    evidence: string;
  };
  acceptance_policy_version: string;
  workflow_run_url: string;
  environment_versions: {
    postgresql: string;
    playwright: string;
    browser: string;
    python: string;
    node: string;
  };
}
```

Contains no test data, entity IDs, or credentials — only digests and outcome attestations.

**Durable persistence**: The compact manifest JSON plus its digest is either:
- Attached to the release candidate as a release asset, or
- Posted as a complete sanitized JSON body to the milestone issue.

A comment containing only an expiring artifact URL is insufficient. Failed manifests remain as failure evidence but never receive an acceptance designation.

Cross-linked to the milestone via GitHub API so it remains discoverable after diagnostic artifacts expire.

## 12. Suite Contracts

Machine-readable `suite-contract.ts` per area E2E suite. CI metadata and acceptance gates — not imported by golden-path runtime.

```typescript
export const suiteContract = {
  schemaVersion: '1.0',
  area: 'scale',
  contractVersion: '1.0.0',
  workspace: 'activated-ephemeral',
  authentication: 'owner-storage-state',
  businessSeed: {
    command: 'seed_prerequisites.py',
    creates: ['confirmed-winner', 'delivery-target', 'policy-chain'],
    mechanism: 'application-command-path',
  },
  adapterModes: ['customer_sandbox', 'controlled_mock'],
  shortcuts: ['library', 'generation', 'assets', 'experiments'],
  goldenPathSteps: [8, 10],
  contractRefs: ['docs/product/scale-advice-semantics-contract.md'],
  specs: ['controlled-mock-journey.spec.ts'],
} as const;
```

`golden-path.spec.ts` is not listed as an area-suite spec. CI validates every contract, confirms declared spec and seed paths exist, and enforces per-run storage-state deletion.

## 13. Prerequisites

### 13.1 ExternalCallCoordinator Refactor

The current Generation handler runs attempt preparation, provider dispatch, and local completion inside one `tenant_session`. Killing it after dispatch can roll back the attempt record needed for reconciliation.

Refactor onto the existing three-phase `ExternalCallCoordinator` seam:

```
committed prepare (attempt, reservation, idempotency key)
→ external dispatch via controlled adapter + sidecar barrier
→ committed completion (or later reconciliation)
```

The three-phase refactor must support controlled-mock execution without resolving or recording provider credentials. Persist the reconciliation key before dispatch. Add an interface for recovering an unfinished attempt.

Do not force the controlled adapter through the existing BYOK credential-binding path.

### 13.2 Atomic Workspace Activation

Add or reuse an application operation equivalent to `activateWorkspaceAndPublish(...)` that transitions the Team to ACTIVE and enqueues the active snapshot in one serializable transaction. A crash cannot leave an ACTIVE Team whose active snapshot was never published.

### 13.3 Provisioning Module Extraction

Extract the shared deep module from the existing `provision-workspace.ts` into `web/lib/control-plane-provisioning/`. Both the production CLI and golden-path command call the same interface. The production CLI becomes explicitly two-phase.
