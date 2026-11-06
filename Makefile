PYTHON ?= .venv/bin/python
ALEMBIC ?= .venv/bin/alembic
NPM ?= npm

.PHONY: test migration-check verify customer-denylist smoke-dev seed-dev \
        web-install web-verify web-migrate db-roles db-release verify-all \
        release-full backup restore-rehearsal \
        compose-up compose-down compose-smoke compose-logs \
        golden-path-up golden-path-run golden-path-down \
        railway-validate-static railway-validate-live

test:
	$(PYTHON) -m pytest -q

migration-check:
	$(ALEMBIC) upgrade head
	$(ALEMBIC) current
	$(ALEMBIC) check

verify: test migration-check

# Full-tree customer denylist scan, e.g. before exporting a public snapshot.
# `make test` already runs the scanner's own tests. Pass the list inline
# (CUSTOMER_DENYLIST="$$(cat data/...)" make customer-denylist); never commit it.
customer-denylist:
	$(PYTHON) scripts/ci/customer_denylist.py --all --require-denylist

smoke-dev:
	$(PYTHON) -m scripts.smoke_business_flow

seed-dev:
	$(PYTHON) -m scripts.seed_dev

# ---------------------------------------------------------------- web ------
# The control plane builds, tests and deploys independently of FastAPI and the
# Worker. `make verify` stays the Python gate; `make verify-all` runs both.

web-install:
	cd web && $(NPM) ci

web-verify:
	cd web && $(NPM) run verify && $(NPM) run inventory:licenses:check

web-migrate:
	cd web && $(NPM) run db:migrate:deploy

db-roles:
	./deploy/postgres/apply-roles.sh

# The supported release path: converge the domain roles, verify the cluster
# Alembic is about to migrate is the one they were converged on, then migrate.
# Never run `alembic upgrade` against a hosted cluster on its own, and note that
# this deliberately does not touch control-plane roles -- see
# deploy/postgres/release.sh.
db-release:
	PYTHON="$(PYTHON)" ALEMBIC="$(ALEMBIC)" ./deploy/postgres/release.sh

# The full release path for the Railway Release service. Runs pre-migration
# backup, control-plane migration (Prisma), then domain migration (Alembic).
# In production this is the Release service entrypoint; locally it validates
# the same sequence against a compose stack.
release-full:
	bash ./deploy/release/release.sh

backup:
	bash ./deploy/release/backup.sh

restore-rehearsal:
	bash ./deploy/release/restore-rehearsal.sh

verify-all: verify web-verify

# -------------------------------------------------------------- compose ------
.PHONY: compose-up compose-down compose-smoke compose-logs compose-security compose-rehearsal compose-redaction golden-path-e2e

compose-up:
	docker compose --env-file deploy/docker/.env.compose up -d --build --wait

compose-down:
	docker compose down -v

compose-smoke: compose-up
	scripts/compose-wait.sh
	scripts/compose-smoke.sh

compose-security: compose-up
	scripts/compose-wait.sh
	scripts/compose-security.sh

compose-rehearsal: compose-up
	scripts/compose-wait.sh
	scripts/compose-migration-rehearsal.sh

compose-redaction: compose-up
	scripts/compose-wait.sh
	scripts/compose-redaction-audit.sh

golden-path-e2e:
	docker compose -p golden-path --env-file deploy/docker/.env.compose up -d --build --wait
	scripts/compose-wait.sh 120 golden-path
	cd web && \
	  CONTROL_PLANE_DATABASE_URL='postgresql://marketai_control_owner:compose_control_owner@localhost:5432/marketai_control' \
	  DOMAIN_OWNER_DATABASE_URL='postgresql://marketai_domain_owner:compose_domain_owner@localhost:5432/marketai_domain' \
	  MARKETAI_DATABASE_URL='postgresql+psycopg://marketai_domain_owner:compose_domain_owner@localhost:5432/marketai_domain' \
	  MARKETAI_TENANT_CONTEXT_SECRET=compose-tenant-context-secret-32-bytes-min \
	  MFA_ENCRYPTION_KEY=Y29tcG9zZS1tZmEtZW5jcnlwdGlvbi1rZXktMzJiISE= \
	  E2E_DOMAIN_BASE_URL='http://localhost:8787' \
	  E2E_BASE_URL='http://localhost:4002' \
	  E2E_COMPOSE_PROJECT=golden-path \
	  E2E_PYTHON='$(PYTHON)' \
	  E2E_GOLDEN_PATH_ALLOW_MUTATIONS=1 \
	  CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN=compose-entitlement-inbox-token \
	  ENTITLEMENT_SNAPSHOT_SIGNING_KEY=compose-entitlement-signing-key-32-bytes-minimum \
	  ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID=compose-beta-v1 \
	  npx playwright test --project golden-path --workers 1 --reporter=html,json
	@echo "Results in web/test-results/ and web/playwright-report/"

compose-logs:
	docker compose logs --no-color

# -------------------------------------------------------------- golden-path ------

golden-path-up:
	docker compose -p gp-local-0 -f docker-compose.yml -f docker-compose.golden-path.yml up -d --build --wait

golden-path-run:
	docker compose -p gp-local-0 -f docker-compose.yml -f docker-compose.golden-path.yml \
	  run --rm --no-deps playwright npx playwright test --config=e2e/golden-path/playwright.config.golden.ts

golden-path-down:
	docker compose -p gp-local-0 -f docker-compose.yml -f docker-compose.golden-path.yml down -v --remove-orphans

# -------------------------------------------------------------- railway ------

railway-validate-static:
	./deploy/railway/validate-static.sh

railway-validate-live:
	./deploy/railway/validate-live.sh
