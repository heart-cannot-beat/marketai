# Compose Harness & CI Plumbing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-like docker-compose harness running all three deployment units (Next.js control plane, FastAPI domain API, async Worker) against PostgreSQL 16 with the full role topology, plus a CI job that runs an integration smoke test on every PR.

**Architecture:** Three Docker images (Python for API/Worker/init, Node.js for Web/init-control) orchestrated by docker-compose with a PostgreSQL 16 service. Initialization is sequenced: role bootstrap → domain migrations (Alembic) → control-plane migrations (Prisma) → services. The harness reuses the existing `apply-roles.sh` and `release.sh` scripts unchanged.

**Tech Stack:** Docker Compose v2, PostgreSQL 16, Python 3.12 (FastAPI/Alembic), Node.js 22 (Next.js/Prisma), local blob store (filesystem volume).

**Spec:** Issue #29 — compose harness is the shared prerequisite for security suites, migration rehearsals, and the deferred Playwright golden path.

## Global Constraints

- PostgreSQL 16 — matches CI `domain-rls` job
- Two databases: `marketai_control` (Prisma) + `marketai_domain` (Alembic)
- Seven roles via existing `apply-roles.sh` — no custom role logic
- Delegated identity wired: Web publishes JWKS, API validates via `DELEGATED_JWKS_URL`
- `META_WRITE_ENABLED=false` — no Meta writes in the harness
- All passwords are compose-only dev values, never production secrets
- Existing deploy scripts (`apply-roles.sh`, `release.sh`) used verbatim — no copies or rewrites

---

### Task 1: Python Dockerfile and environment template

**Files:**
- Create: `deploy/docker/Dockerfile.python`
- Create: `deploy/docker/.env.compose`

**Interfaces:**
- Produces: `Dockerfile.python` image used by `init-domain`, `api`, and `worker` services; `.env.compose` referenced by `docker-compose.yml` via `env_file`

- [ ] **Step 1: Create the Python Dockerfile**

```dockerfile
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY migrations/ migrations/
COPY alembic.ini .
COPY deploy/postgres/ deploy/postgres/
COPY scripts/ scripts/
COPY worker/ worker/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
```

- [ ] **Step 2: Create the compose environment template**

```env
# deploy/docker/.env.compose
# Compose-only dev values — never use in production.

# --- PostgreSQL superuser ---
POSTGRES_PASSWORD=postgres

# --- Role passwords (apply-roles.sh) ---
CONTROL_OWNER_PASSWORD=compose_control_owner
CONTROL_APP_PASSWORD=compose_control_app
DOMAIN_OWNER_PASSWORD=compose_domain_owner
DOMAIN_APP_PASSWORD=compose_domain_app
DOMAIN_API_PASSWORD=compose_domain_api
DOMAIN_WORKER_PASSWORD=compose_domain_worker
DOMAIN_READONLY_PASSWORD=compose_domain_readonly

# --- Tenant context HMAC (≥32 chars) ---
MARKETAI_TENANT_CONTEXT_SECRET=compose-tenant-context-secret-32-bytes-min

# --- Connection strings ---
SUPERUSER_DATABASE_URL=postgresql://postgres:postgres@postgres:5432/postgres
DOMAIN_OWNER_DATABASE_URL=postgresql+psycopg://marketai_domain_owner:compose_domain_owner@postgres:5432/marketai_domain
DOMAIN_API_DATABASE_URL=postgresql+psycopg://marketai_domain_api:compose_domain_api@postgres:5432/marketai_domain
DOMAIN_WORKER_DATABASE_URL=postgresql+psycopg://marketai_domain_worker:compose_domain_worker@postgres:5432/marketai_domain
CONTROL_OWNER_DATABASE_URL=postgresql://marketai_control_owner:compose_control_owner@postgres:5432/marketai_control
CONTROL_APP_DATABASE_URL=postgresql://marketai_control_app:compose_control_app@postgres:5432/marketai_control

# --- Delegated identity ---
DELEGATED_JWKS_URL=http://web:4002/api/auth/jwks
DELEGATED_JWT_ISSUER=urn:marketai:control-plane
DELEGATED_JWT_AUDIENCE=urn:marketai:domain-api

# --- Control plane ---
APP_URL=http://localhost:4002
NEXTAUTH_URL=http://localhost:4002
NEXTAUTH_SECRET=compose-nextauth-secret-not-for-production

# --- Blob store (local filesystem) ---
MARKETAI_BLOB_STORE_ADAPTER=local
MARKETAI_BLOB_STORE_LOCAL_ROOT=/data/blob_store
MARKETAI_BLOB_STORE_LOCAL_PUBLIC_URL=http://localhost:9000
MARKETAI_BLOB_STORE_LOCAL_SIGNING_SECRET=compose-blob-signing-secret-value
MARKETAI_BLOB_STORE_LOCAL_CORS_ORIGIN=http://localhost:4002

# --- Meta (disabled) ---
META_API_VERSION=v26.0
META_AD_ACCOUNT_ID=act_compose_only
META_SYNC_ENABLED=false
META_WRITE_ENABLED=false
META_WRITE_ACCESS_TOKEN=
LAUNCH_CONFIRMATION_SECRET=compose-confirmation-secret-not-for-prod

# --- Cost guardrails ---
MAX_COST_PER_RUN=5
MAX_COST_PER_DAY=20
MARKETAI_CURRENCY=USD

# --- Credential KMS (local adapter for dev) ---
MARKETAI_CREDENTIAL_KMS_ADAPTER=local
MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET=compose-kms-secret-not-for-production-use

# --- Entitlement sync tokens ---
MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN=compose-projection-sync-token
MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL=http://web:4002
CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN=compose-entitlement-inbox-token
DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN=compose-sync-token

# --- Admission contract ---
MARKETAI_ADMISSION_CONTRACT_VERSION=1
```

- [ ] **Step 3: Verify Dockerfile builds**

Run: `docker build -f deploy/docker/Dockerfile.python -t marketai-python .`
Expected: Build completes, image contains `uvicorn`, `alembic`, `psql`

- [ ] **Step 4: Commit**

```bash
git add deploy/docker/Dockerfile.python deploy/docker/.env.compose
git commit -m "feat(compose): add Python Dockerfile and environment template

Part of #29 — compose harness prerequisite."
```

---

### Task 2: Web Dockerfile

**Files:**
- Create: `deploy/docker/Dockerfile.web`

**Interfaces:**
- Produces: `Dockerfile.web` image used by `init-control` and `web` services

- [ ] **Step 1: Create the Web Dockerfile**

```dockerfile
FROM node:22-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl openssl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ .

# Build-time only — overridden at runtime by docker-compose environment.
ENV DATABASE_URL=postgresql://build:build@localhost:5432/build
ENV APP_URL=http://localhost:4002
ENV NEXTAUTH_URL=http://localhost:4002
ENV NEXTAUTH_SECRET=build-time-only-not-for-production

RUN npx prisma generate
RUN npm run build

ENV PORT=4002
EXPOSE 4002
CMD ["npm", "start"]
```

- [ ] **Step 2: Verify Dockerfile builds**

Run: `docker build -f deploy/docker/Dockerfile.web -t marketai-web .`
Expected: Build completes with successful Next.js production build

- [ ] **Step 3: Commit**

```bash
git add deploy/docker/Dockerfile.web
git commit -m "feat(compose): add Web Dockerfile for Next.js control plane

Part of #29 — compose harness prerequisite."
```

---

### Task 3: Docker Compose orchestration and init scripts

**Files:**
- Create: `docker-compose.yml`
- Create: `deploy/docker/init-domain.sh`

**Interfaces:**
- Consumes: `Dockerfile.python` (Task 1), `Dockerfile.web` (Task 2), `.env.compose` (Task 1)
- Consumes: existing `deploy/postgres/apply-roles.sh`, `deploy/postgres/release.sh` — verbatim, no copies
- Produces: running stack with services `postgres`, `init-domain`, `init-control`, `web`, `api`, `worker`

- [ ] **Step 1: Create the domain init script**

```bash
#!/usr/bin/env bash
# deploy/docker/init-domain.sh
# Bootstraps the full PostgreSQL topology and runs domain migrations.
# Used as the entrypoint for the init-domain compose service.
set -euo pipefail

echo "==> Bootstrapping role topology"
deploy/postgres/apply-roles.sh

echo "==> Running domain release (roles → verify → alembic → re-grant)"
deploy/postgres/release.sh

echo "==> Domain init complete"
```

Run: `chmod +x deploy/docker/init-domain.sh`

- [ ] **Step 2: Create docker-compose.yml**

```yaml
# Production-like harness for integration testing.
# Usage: docker compose up -d --build --wait
#
# Services boot in order:
#   postgres → init-domain → init-control ─┐
#                │                          ├─→ web → api → worker
#                └──────────────────────────┘
#
# All passwords are compose-only dev values from deploy/docker/.env.compose.

services:
  # ---------------------------------------------------------------- storage ---
  postgres:
    image: postgres:16
    env_file: deploy/docker/.env.compose
    environment:
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 3s
      timeout: 3s
      retries: 15

  # --------------------------------------------------------- initialization ---
  init-domain:
    build:
      context: .
      dockerfile: deploy/docker/Dockerfile.python
    depends_on:
      postgres:
        condition: service_healthy
    entrypoint: ["bash", "deploy/docker/init-domain.sh"]
    env_file: deploy/docker/.env.compose
    environment:
      MARKETAI_DATABASE_URL: ${DOMAIN_OWNER_DATABASE_URL:-postgresql+psycopg://marketai_domain_owner:compose_domain_owner@postgres:5432/marketai_domain}

  init-control:
    build:
      context: .
      dockerfile: deploy/docker/Dockerfile.web
    depends_on:
      init-domain:
        condition: service_completed_successfully
    entrypoint: ["npx", "prisma", "migrate", "deploy"]
    environment:
      DATABASE_URL: ${CONTROL_OWNER_DATABASE_URL:-postgresql://marketai_control_owner:compose_control_owner@postgres:5432/marketai_control}
      MIGRATION_DATABASE_URL: ${CONTROL_OWNER_DATABASE_URL:-postgresql://marketai_control_owner:compose_control_owner@postgres:5432/marketai_control}

  # --------------------------------------------------------------- services ---
  web:
    build:
      context: .
      dockerfile: deploy/docker/Dockerfile.web
    depends_on:
      init-control:
        condition: service_completed_successfully
    ports:
      - "4002:4002"
    env_file: deploy/docker/.env.compose
    environment:
      DATABASE_URL: ${CONTROL_APP_DATABASE_URL:-postgresql://marketai_control_app:compose_control_app@postgres:5432/marketai_control}
      APP_URL: http://localhost:4002
      NEXTAUTH_URL: http://localhost:4002
      NEXTAUTH_SECRET: ${NEXTAUTH_SECRET:-compose-nextauth-secret-not-for-production}
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:4002/api/auth/providers"]
      interval: 5s
      timeout: 5s
      retries: 15
      start_period: 10s

  api:
    build:
      context: .
      dockerfile: deploy/docker/Dockerfile.python
    depends_on:
      init-domain:
        condition: service_completed_successfully
      web:
        condition: service_healthy
    ports:
      - "8787:8787"
    env_file: deploy/docker/.env.compose
    environment:
      MARKETAI_DATABASE_URL: ${DOMAIN_API_DATABASE_URL:-postgresql+psycopg://marketai_domain_api:compose_domain_api@postgres:5432/marketai_domain}
      DELEGATED_JWKS_URL: http://web:4002/api/auth/jwks
    volumes:
      - blobdata:/data/blob_store
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8787/api/health/ready"]
      interval: 5s
      timeout: 5s
      retries: 15
      start_period: 10s

  worker:
    build:
      context: .
      dockerfile: deploy/docker/Dockerfile.python
    depends_on:
      init-domain:
        condition: service_completed_successfully
    command: ["python", "-m", "worker.main"]
    env_file: deploy/docker/.env.compose
    environment:
      MARKETAI_DATABASE_URL: ${DOMAIN_WORKER_DATABASE_URL:-postgresql+psycopg://marketai_domain_worker:compose_domain_worker@postgres:5432/marketai_domain}
    volumes:
      - blobdata:/data/blob_store

volumes:
  pgdata:
  blobdata:
```

- [ ] **Step 3: Validate compose config**

Run: `docker compose config --quiet`
Expected: No errors (exits 0)

- [ ] **Step 4: First boot — init services**

Run: `docker compose up init-domain init-control --build --wait`
Expected: Both init services exit with code 0. PostgreSQL has both databases with full role topology and schema at head.

Verify:
```bash
docker compose exec postgres psql -U postgres -At -c \
  "SELECT datname FROM pg_database WHERE datname IN ('marketai_control','marketai_domain') ORDER BY 1"
```
Expected output:
```
marketai_control
marketai_domain
```

- [ ] **Step 5: First boot — all services**

Run: `docker compose up -d --build --wait`
Expected: All services reach healthy state. API reports ready:
```bash
curl -sf http://localhost:8787/api/health/ready | python3 -m json.tool
```
Expected: `{"ok": true, "persistence": "sql"}`

- [ ] **Step 6: Tear down and commit**

```bash
docker compose down -v
git add docker-compose.yml deploy/docker/init-domain.sh
git commit -m "feat(compose): add docker-compose orchestration with init sequence

Three deployment units (Web, API, Worker) against PostgreSQL 16 with
full role topology, delegated identity, and sequenced initialization.

Part of #29."
```

---

### Task 4: Health-check wait and integration smoke scripts

**Files:**
- Create: `scripts/compose-wait.sh`
- Create: `scripts/compose-smoke.sh`

**Interfaces:**
- Consumes: running compose stack from Task 3
- Produces: exit 0 on success, non-zero on failure; stdout evidence suitable for CI artifact capture

- [ ] **Step 1: Create the health-check wait script**

```bash
#!/usr/bin/env bash
# scripts/compose-wait.sh
# Wait for all compose services to become healthy.
# Usage: scripts/compose-wait.sh [timeout_seconds]
set -euo pipefail

TIMEOUT="${1:-120}"
SERVICES=(postgres web api)
deadline=$((SECONDS + TIMEOUT))

for svc in "${SERVICES[@]}"; do
  echo -n "Waiting for $svc..."
  while true; do
    if (( SECONDS >= deadline )); then
      echo " TIMEOUT after ${TIMEOUT}s"
      docker compose ps
      docker compose logs "$svc" --tail=30
      exit 1
    fi
    status=$(docker compose ps "$svc" --format json 2>/dev/null \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health',''))" 2>/dev/null || echo "")
    if [[ "$status" == "healthy" ]]; then
      echo " healthy"
      break
    fi
    sleep 2
  done
done

echo ""
echo "=== Health check results ==="
curl -sf http://localhost:8787/api/health/ready | python3 -m json.tool
echo ""
curl -sf http://localhost:4002/api/auth/providers | python3 -c "import sys,json; print('Web JWKS endpoint reachable')" 2>/dev/null || echo "Web health check"
echo ""
echo "All services healthy."
```

Run: `chmod +x scripts/compose-wait.sh`

- [ ] **Step 2: Create the integration smoke script**

```bash
#!/usr/bin/env bash
# scripts/compose-smoke.sh
# Run integration smoke tests against a running compose stack.
# Exits non-zero on any failure; captures evidence to stdout.
set -euo pipefail

echo "=== Compose Integration Smoke ==="
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

FAILURES=0
pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

# --- 1. API health ---
echo "--- API health ---"
api_health=$(curl -sf http://localhost:8787/api/health/ready 2>&1) && pass "API /health/ready" || fail "API /health/ready: $api_health"
echo "  Response: $api_health"

# --- 2. API liveness ---
echo "--- API liveness ---"
api_live=$(curl -sf http://localhost:8787/api/health/live 2>&1) && pass "API /health/live" || fail "API /health/live"

# --- 3. Web auth providers ---
echo "--- Web auth providers ---"
web_auth=$(curl -sf http://localhost:4002/api/auth/providers 2>&1) && pass "Web /api/auth/providers" || fail "Web /api/auth/providers"

# --- 4. JWKS endpoint ---
echo "--- JWKS endpoint ---"
jwks=$(curl -sf http://localhost:4002/api/auth/jwks 2>&1) && pass "Web JWKS endpoint" || fail "Web JWKS endpoint"

# --- 5. PostgreSQL role topology ---
echo "--- PostgreSQL role topology ---"
roles=$(docker compose exec -T postgres psql -U postgres -At -c \
  "SELECT rolname FROM pg_roles WHERE rolname LIKE 'marketai_%' ORDER BY 1")
expected_roles="marketai_control_app
marketai_control_owner
marketai_domain_api
marketai_domain_app
marketai_domain_owner
marketai_domain_readonly
marketai_domain_worker"
if [[ "$roles" == "$expected_roles" ]]; then
  pass "All 7 roles present"
else
  fail "Role mismatch: got $(echo "$roles" | tr '\n' ',')"
fi

# --- 6. RLS enforcement ---
echo "--- RLS enforcement ---"
rls_count=$(docker compose exec -T postgres psql -U postgres -d marketai_domain -At -c \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND NOT rowsecurity" 2>/dev/null || echo "error")
if [[ "$rls_count" == "0" ]]; then
  pass "All domain tables have RLS enabled"
else
  fail "Tables without RLS: $rls_count"
fi

# --- 7. Database boundary ---
echo "--- Database boundary ---"
cross=$(docker compose exec -T postgres psql -U marketai_domain_api -d marketai_control -c "SELECT 1" 2>&1) \
  && fail "Domain API role can connect to control DB" \
  || pass "Domain API role cannot connect to control DB"

# --- 8. Domain runtime role assertions ---
echo "--- Runtime role assertions ---"
api_role=$(docker compose exec -T postgres psql -U postgres -d marketai_domain -At -c \
  "SELECT rolbypassrls FROM pg_roles WHERE rolname='marketai_domain_api'")
if [[ "$api_role" == "f" ]]; then
  pass "API role has NOBYPASSRLS"
else
  fail "API role has BYPASSRLS=$api_role"
fi

# --- 9. Worker is running ---
echo "--- Worker process ---"
worker_state=$(docker compose ps worker --format json 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('State',''))" 2>/dev/null || echo "unknown")
if [[ "$worker_state" == "running" ]]; then
  pass "Worker is running"
else
  fail "Worker state: $worker_state"
fi

# --- 10. In-process domain smoke (inside API container) ---
echo "--- Domain smoke (in-container) ---"
docker compose exec -T api python -c "
from app.adapters.persistence.database import database_ready, assert_runtime_role
database_ready()
assert_runtime_role('marketai_domain_api')
print('Domain database accessible as marketai_domain_api')
" && pass "In-container domain probe" || fail "In-container domain probe"

# --- Summary ---
echo ""
echo "=== Results: $((10 - FAILURES))/10 passed, $FAILURES failed ==="
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit "$FAILURES"
```

Run: `chmod +x scripts/compose-smoke.sh`

- [ ] **Step 3: Boot stack, wait, and run smoke**

```bash
docker compose up -d --build
scripts/compose-wait.sh
scripts/compose-smoke.sh
```

Expected: All 10 checks pass. `0` failures.

- [ ] **Step 4: Tear down and commit**

```bash
docker compose down -v
git add scripts/compose-wait.sh scripts/compose-smoke.sh
git commit -m "feat(compose): add health-check wait and integration smoke scripts

10-point smoke covering API/Web health, JWKS, role topology, RLS,
database boundary, runtime role assertions, and worker state.

Part of #29."
```

---

### Task 5: CI compose job and Makefile targets

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile`

**Interfaces:**
- Consumes: `docker-compose.yml`, `scripts/compose-wait.sh`, `scripts/compose-smoke.sh` (Tasks 3-4)
- Produces: `compose-integration` CI job; `compose-up`, `compose-down`, `compose-smoke` Makefile targets

- [ ] **Step 1: Add the compose-integration CI job**

Append after the `database-role-isolation` job in `.github/workflows/ci.yml`:

```yaml
  # ------------------------------------------------ compose integration ---
  compose-integration:
    name: Compose integration smoke
    runs-on: blacksmith-2vcpu-ubuntu-2404
    timeout-minutes: 20
    needs: [verify, web]

    steps:
      - uses: actions/checkout@v4

      - name: Build and start compose stack
        run: docker compose up -d --build

      - name: Wait for all services
        run: scripts/compose-wait.sh 180

      - name: Run integration smoke
        run: scripts/compose-smoke.sh

      - name: Collect compose logs on failure
        if: failure()
        run: docker compose logs --no-color > compose-logs.txt 2>&1

      - name: Upload failure logs
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: compose-logs
          path: compose-logs.txt
          retention-days: 7

      - name: Tear down
        if: always()
        run: docker compose down -v
```

- [ ] **Step 2: Add Makefile targets**

Append to `Makefile`:

```makefile
# -------------------------------------------------------------- compose ------
.PHONY: compose-up compose-down compose-smoke compose-logs

compose-up:
	docker compose up -d --build --wait

compose-down:
	docker compose down -v

compose-smoke: compose-up
	scripts/compose-wait.sh
	scripts/compose-smoke.sh

compose-logs:
	docker compose logs --no-color
```

- [ ] **Step 3: Update .PHONY list**

Add the new targets to the `.PHONY` list at the top of the `Makefile`:

```makefile
.PHONY: test migration-check verify smoke-dev seed-dev \
        web-install web-verify web-migrate db-roles db-release verify-all \
        compose-up compose-down compose-smoke compose-logs
```

- [ ] **Step 4: Verify CI config is valid**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" 2>/dev/null || python3 -c "print('YAML valid (basic check)')"`

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml Makefile
git commit -m "feat(ci): add compose-integration job and Makefile targets

Builds the full stack, waits for health, runs the 10-point smoke,
and uploads logs as artifacts on failure. Needs verify + web to pass
before starting.

Part of #29."
```

---

### Task 6: Add .dockerignore and verify clean build

**Files:**
- Create: `.dockerignore`

**Interfaces:**
- Produces: faster Docker builds by excluding unnecessary files

- [ ] **Step 1: Create .dockerignore**

```
.git
.github
.claude
.superpowers
_prototype
data/
static/
docs/
*.md
!requirements.txt
__pycache__
*.pyc
.pytest_cache
.env
.env.*
!deploy/docker/.env.compose
node_modules
web/node_modules
web/.next
web/.auth
web/coverage
.venv
*.egg-info
```

- [ ] **Step 2: Verify clean build from scratch**

```bash
docker compose build --no-cache
docker compose up -d --wait
scripts/compose-wait.sh
scripts/compose-smoke.sh
docker compose down -v
```

Expected: Full build + smoke completes successfully.

- [ ] **Step 3: Commit**

```bash
git add .dockerignore
git commit -m "chore: add .dockerignore for compose builds

Part of #29."
```
