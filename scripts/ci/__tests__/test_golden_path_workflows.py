"""Regression checks for the executable golden-path workflow contract."""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
GOLDEN_PATH_WORKFLOWS = (
    REPO / ".github" / "workflows" / "golden-path-pr.yml",
    REPO / ".github" / "workflows" / "golden-path-acceptance.yml",
)


def _golden_path_job(workflow: Path) -> str:
    text = workflow.read_text(encoding="utf-8")
    job = text.split("\n  golden-path:\n", 1)[1]
    if workflow.name == "golden-path-acceptance.yml":
        job = job.split("\n  assemble:\n", 1)[0]
    return job


def test_golden_path_workflows_build_broker_before_starting_it() -> None:
    for workflow in GOLDEN_PATH_WORKFLOWS:
        job = _golden_path_job(workflow)
        install = job.index("npm ci --prefix test-support/lifecycle-broker")
        build = job.index("npm run build --prefix test-support/lifecycle-broker")
        socket = job.index('BROKER_SOCKET="$BROKER_SOCKET_DIR/broker.sock"')
        start = job.index("node test-support/lifecycle-broker/dist/broker.js")
        assert install < build < socket < start, workflow


def test_golden_path_workflows_install_python_dependencies_before_validation() -> None:
    for workflow in GOLDEN_PATH_WORKFLOWS:
        job = _golden_path_job(workflow)
        setup = job.index("uses: actions/setup-python@v5")
        install = job.index(
            "python -m pip install --disable-pip-version-check -r requirements.txt"
        )
        validate = job.index("python3 scripts/ci/validate_topology.py")
        assert setup < install < validate, workflow


def test_golden_path_workflows_wait_on_declared_container_health() -> None:
    for workflow in GOLDEN_PATH_WORKFLOWS:
        job = _golden_path_job(workflow)
        assert "docker inspect --format=" in job, workflow
        assert '"$svc" == "worker" && "$state" == "running"' in job, workflow
        assert "exec -T \\$svc wget" not in job, workflow


def test_golden_path_workflows_use_current_artifact_cli_contracts() -> None:
    for workflow in GOLDEN_PATH_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert '--project-name "$COMPOSE_PROJECT"' in text, workflow
        assert "--raw-input /tmp/raw-compose-logs.txt" in text, workflow
        assert (
            '--output "$ARTIFACT_DIR/logs/compose-logs-redacted.txt"' in text
        ), workflow
        assert '--artifact-dir "$ARTIFACT_DIR"' in text, workflow


def test_golden_path_workflows_use_current_topology_cli_contract() -> None:
    for workflow in GOLDEN_PATH_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert "--phase config" in text, workflow
        assert "--phase live" in text, workflow
        assert '--project-name "$COMPOSE_PROJECT"' in text, workflow
        assert "--compose-file docker-compose.yml" in text, workflow
        assert "--compose-file docker-compose.golden-path.yml" in text, workflow
        assert (
            '--private-key "$TOPOLOGY_ATTESTATION_DIR/private.pem"' in text
        ), workflow
        assert (
            '--write-signed-attestation '
            '"$TOPOLOGY_ATTESTATION_DIR/attestation.json"' in text
        ), workflow


def test_control_plane_ci_typechecks_e2e_sources() -> None:
    workflow = REPO / ".github" / "workflows" / "ci.yml"
    assert "npm run check-types:e2e" in workflow.read_text(encoding="utf-8")


def test_deterministic_golden_path_is_a_required_ci_gate() -> None:
    workflow = REPO / ".github" / "workflows" / "ci.yml"
    text = workflow.read_text(encoding="utf-8")
    job = text.split("\n  golden-path-e2e:\n", 1)[1].split(
        "\n  # ---------------------------------------------------- secret scan", 1
    )[0]
    assert "continue-on-error" not in job


def test_deterministic_journey_authenticates_direct_bff_mutations() -> None:
    journey = (
        REPO / "web" / "e2e" / "golden-path" / "full-journey.spec.ts"
    ).read_text(encoding="utf-8")
    fixtures = (
        REPO / "web" / "e2e" / "golden-path" / "fixtures.ts"
    ).read_text(encoding="utf-8")

    direct_mutations = re.findall(
        r"page\.request\.(?:post|put|patch|delete)\(", journey
    )
    protected_mutations = journey.count(
        "headers: await domainMutationHeaders(page"
    )

    assert protected_mutations == len(direct_mutations)
    assert "Origin: new URL(page.url()).origin" in fixtures
    assert "'X-CSRF-Token': token" in fixtures
    assert "page.request.get('/api/csrf')" in fixtures


def test_deterministic_journey_scopes_publish_confirmation_to_its_dialog() -> None:
    journey = (
        REPO / "web" / "e2e" / "golden-path" / "full-journey.spec.ts"
    ).read_text(encoding="utf-8")

    confirmations = journey.count("const confirmPublish")
    scoped_dialogs = journey.count(
        "getByRole('dialog', { name: /publish entry/i })"
    )
    assert confirmations > 0
    assert scoped_dialogs == confirmations


def test_deterministic_journey_uses_detail_headings_after_library_reload() -> None:
    journey = (
        REPO / "web" / "e2e" / "golden-path" / "full-journey.spec.ts"
    ).read_text(encoding="utf-8")

    # Every library entity waits for its detail heading twice: once after
    # creation and once after the library reload. Count per entity so other
    # headings (for example the exact experiment heading) do not skew it.
    library_entities = (
        "GP E2E Product",
        "GP E2E Offer",
        "GP E2E Template",
        "GP E2E Policy",
        "GP E2E Person",
        "GP E2E Data Source",
    )
    for entity in library_entities:
        heading = f"getByRole('heading', {{ name: '{entity}' }})"
        assert journey.count(heading) == 2, entity


def test_deterministic_journey_uses_supported_dimension_cardinality() -> None:
    journey = (
        REPO / "web" / "e2e" / "golden-path" / "full-journey.spec.ts"
    ).read_text(encoding="utf-8")

    assert "combination_cardinality: 'one_per_dimension'" in journey
    assert "combination_cardinality: 'full'" not in journey


def test_container_journey_fails_fast_on_missing_ui_contracts() -> None:
    config = (
        REPO
        / "web"
        / "e2e"
        / "golden-path"
        / "playwright.config.golden.ts"
    ).read_text(encoding="utf-8")

    assert "actionTimeout: 15_000" in config
    assert "navigationTimeout: 30_000" in config


def test_deterministic_journey_fails_fast_on_missing_ui_contracts() -> None:
    config = (REPO / "web" / "playwright.config.ts").read_text(
        encoding="utf-8"
    )
    golden_project = config.split("name: 'golden-path'", 1)[1]

    assert "actionTimeout: 15_000" in golden_project
    assert "navigationTimeout: 30_000" in golden_project


def test_deterministic_journey_uses_the_people_navigation_label() -> None:
    journey = (
        REPO / "web" / "e2e" / "golden-path" / "full-journey.spec.ts"
    ).read_text(encoding="utf-8")

    assert "getByRole('button', { name: /people/i })" in journey
    assert "getByRole('button', { name: /person/i })" not in journey


def test_deterministic_journey_imports_manual_facts_through_upload_contract() -> None:
    journey = (
        REPO / "web" / "e2e" / "golden-path" / "full-journey.spec.ts"
    ).read_text(encoding="utf-8")

    choose_file = journey.index(
        "getByLabel(/select fact file to import/i).setInputFiles"
    )
    # The journey waits for the panel's success sentence, read from the en
    # locale: `web/__tests__/locales/e2e-copy.spec.ts` keeps e2e text on
    # locale copy, and #14 reworded the sentence.
    success = "en('library', 'data-source-ops.import-complete')"
    assert journey.index(success) > choose_file
    library = json.loads(
        (REPO / "web" / "locales" / "en" / "library.json").read_text(
            encoding="utf-8"
        )
    )
    assert library["data-source-ops"]["import-complete"]
    assert "/manual-fact-imports`" not in journey


def test_container_journey_has_least_privilege_control_plane_bootstrap() -> None:
    compose = (REPO / "docker-compose.golden-path.yml").read_text(
        encoding="utf-8"
    )
    playwright = compose.split("\n  playwright:\n", 1)[1].split(
        "\nnetworks:\n", 1
    )[0]
    provisioning = (
        REPO / "web" / "scripts" / "golden-path-provisioning.ts"
    ).read_text(encoding="utf-8")

    assert (
        "DATABASE_URL: postgresql://marketai_control_app:"
        "compose_control_app@postgres:5432/marketai_control"
    ) in playwright
    assert "marketai_control_owner" not in playwright
    assert "prisma.planVersion.findUnique" in provisioning
    assert "controlled_mock: true" in provisioning


def test_container_evidence_inspector_receives_the_tenant_context_secret() -> None:
    compose = (REPO / "docker-compose.golden-path.yml").read_text(
        encoding="utf-8"
    )
    playwright = compose.split("\n  playwright:\n", 1)[1].split(
        "\nnetworks:\n", 1
    )[0]
    context_secret = _compose_env()["MARKETAI_TENANT_CONTEXT_SECRET"]

    assert (
        f"MARKETAI_TENANT_CONTEXT_SECRET: {context_secret}" in playwright
    )


def test_container_provisioning_uses_the_ts_node_commonjs_hook() -> None:
    fixture = (
        REPO / "web" / "e2e" / "golden-path" / "fixtures" / "provisioning.ts"
    ).read_text(encoding="utf-8")

    assert "'--eval'" in fixture
    assert "require(process.argv[1])" in fixture
    assert "TS_NODE_COMPILER_OPTIONS" in fixture


def test_container_provisioning_materializes_the_operator_user_before_begin() -> None:
    provisioning = (
        REPO / "web" / "scripts" / "golden-path-provisioning.ts"
    ).read_text(encoding="utf-8")

    operator = provisioning.index("prisma.user.upsert")
    begin = provisioning.index("beginProvisioning(prisma, input)")
    assert operator < begin


def test_container_onboarding_contexts_inherit_the_configured_base_url() -> None:
    harness = (
        REPO
        / "web"
        / "e2e"
        / "golden-path"
        / "fixtures"
        / "onboarding-harness.ts"
    ).read_text(encoding="utf-8")

    assert harness.count("baseURL: this.baseUrl") == harness.count(
        "this.browser.newContext({"
    )


def test_container_onboarding_consumes_invites_and_enrolls_owner_mfa() -> None:
    harness = (
        REPO
        / "web"
        / "e2e"
        / "golden-path"
        / "fixtures"
        / "onboarding-harness.ts"
    ).read_text(encoding="utf-8")

    assert "input[name=\"name\"]" in harness
    assert "page.request.get(" in harness
    assert "Invitation preflight failed with status" in harness
    assert "Create Account" in harness
    assert "input[name=\"email\"]" in harness
    assert "input[name=\"mfaCode\"]" in harness
    assert "Set up MFA" in harness
    assert "Confirm MFA" in harness


def test_container_journey_activates_workspace_before_member_invitation() -> None:
    journey = (
        REPO / "web" / "e2e" / "golden-path" / "golden-path.spec.ts"
    ).read_text(encoding="utf-8")

    complete = journey.index("provisioning.complete({ provisioningRef })")
    invite = journey.index("Create Member invitation")
    assert complete < invite


def test_golden_path_compose_has_its_declared_playwright_dockerfile() -> None:
    compose = (REPO / "docker-compose.golden-path.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = REPO / "web" / "e2e" / "golden-path" / "Dockerfile"
    assert dockerfile.is_file()
    assert "dockerfile: web/e2e/golden-path/Dockerfile" in compose
    assert "mcr.microsoft.com/playwright:v1.62.1-noble" in dockerfile.read_text(
        encoding="utf-8"
    )


def test_golden_path_control_artifacts_are_bound_into_playwright() -> None:
    compose = (REPO / "docker-compose.golden-path.yml").read_text(
        encoding="utf-8"
    )
    validator = (REPO / "scripts" / "ci" / "validate_topology.py").read_text(
        encoding="utf-8"
    )
    assert "source: ${BROKER_SOCKET_DIR:-/var/run/lifecycle-broker}" in compose
    assert "source: ${TOPOLOGY_ATTESTATION_DIR:-/var/run/topology}" in compose
    assert "broker-socket:" not in compose
    assert "topology-attestation:" not in compose
    assert '"/var/run/lifecycle-broker"' in validator
    assert '"/var/run/topology"' in validator

    for workflow in GOLDEN_PATH_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert "BROKER_SOCKET_DIR: /var/run/lifecycle-broker" in text
        assert "TOPOLOGY_ATTESTATION_DIR: /var/run/topology" in text
        assert (
            "MAIL_SINK_TOKEN_FILE: "
            "${{ github.workspace }}/secrets/mail_sink_token.txt"
        ) in text
        assert 'sudo install -d -m 0777 "$TOPOLOGY_ATTESTATION_DIR"' in text


def test_golden_path_results_are_bound_back_to_the_host_verifier() -> None:
    compose = (REPO / "docker-compose.golden-path.yml").read_text(
        encoding="utf-8"
    )

    assert "source: ${ARTIFACT_DIR:-/tmp/gp-artifacts}" in compose
    assert "target: /results" in compose
    assert "playwright-results:" not in compose
    validator = (REPO / "scripts" / "ci" / "validate_topology.py").read_text(
        encoding="utf-8"
    )
    assert '"/tmp/gp-artifacts"' in validator

    for workflow in GOLDEN_PATH_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert (
            'sudo install -d -m 0777 "$TOPOLOGY_ATTESTATION_DIR" '
            '"$BROKER_SOCKET_DIR" "$ARTIFACT_DIR"'
        ) in text


def test_golden_path_playwright_only_starts_when_explicitly_targeted() -> None:
    compose = (REPO / "docker-compose.golden-path.yml").read_text(
        encoding="utf-8"
    )
    playwright = compose.split("\n  playwright:\n", 1)[1].split(
        "\nnetworks:\n", 1
    )[0]

    assert "profiles:\n      - test-runner" in playwright
    for workflow in GOLDEN_PATH_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert "run --rm" in text
        assert "playwright" in text


def test_container_journey_marks_its_evidence_report_complete() -> None:
    journey = (
        REPO / "web" / "e2e" / "golden-path" / "golden-path.spec.ts"
    ).read_text(encoding="utf-8")

    assert "completion.pass()" in journey
    assert "artifacts.markPassed()" in journey


def test_container_journey_uses_the_unlabelled_member_email_contract() -> None:
    journey = (
        REPO / "web" / "e2e" / "golden-path" / "golden-path.spec.ts"
    ).read_text(encoding="utf-8")

    assert "getByPlaceholder('teammate@example.com')" in journey
    assert "getByRole('button', { name: /^invite$/i })" in journey
    assert "getByLabel(/email/i)" not in journey


def test_golden_path_sidecars_build_typescript_before_runtime() -> None:
    for service in ("controlled-provider", "mail-sink"):
        root = REPO / "test-support" / service
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        package = (root / "package.json").read_text(encoding="utf-8")
        assert '"build": "tsc --project tsconfig.json"' in package
        assert "RUN npm run build" in dockerfile
        assert 'CMD ["node", "dist/server.js"]' in dockerfile


def test_mail_sink_secret_is_consumed_from_mounted_file() -> None:
    compose = (REPO / "docker-compose.golden-path.yml").read_text(
        encoding="utf-8"
    )
    mail_sink = (REPO / "test-support" / "mail-sink" / "server.ts").read_text(
        encoding="utf-8"
    )
    spec = (
        REPO / "web" / "e2e" / "golden-path" / "golden-path.spec.ts"
    ).read_text(encoding="utf-8")
    assert compose.count("MAIL_SINK_TOKEN_FILE: /run/secrets/mail_sink_token") == 2
    assert "fs.readFileSync(tokenFile" in mail_sink
    assert "fs.readFileSync(tokenFile" in spec


def test_golden_path_workflows_preserve_artifact_safety_gates() -> None:
    verifier = (REPO / "scripts" / "ci" / "verify_golden_artifacts.py").read_text(
        encoding="utf-8"
    )
    assert "findings.extend(check_allowlist(artifact_dir))" in verifier
    assert "findings.extend(check_secrets(artifact_dir))" in verifier

    for workflow in GOLDEN_PATH_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        redact = text.index("python3 scripts/ci/redact_compose_logs.py")
        verify = text.index("python3 scripts/ci/verify_golden_artifacts.py")
        upload = text.index("uses: actions/upload-artifact@v4")
        assert redact < verify < upload, workflow
        assert "if: always() && steps.verify.outcome == 'success'" in text


def _compose_env() -> dict[str, str]:
    env: dict[str, str] = {}
    lines = (REPO / "deploy" / "docker" / ".env.compose").read_text(
        encoding="utf-8"
    ).splitlines()
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    return env


def test_compose_api_can_pull_workspace_projection_from_web() -> None:
    # The golden path provisions its Workspace through the control plane, so
    # the domain API learns about it on the first delegated request by
    # pulling the authorization snapshot from web
    # (app/platform/projection_sync.py). web serves that pull only when its
    # PROJECTION_SYNC_SERVICE_TOKEN matches the token the API sends; without
    # it every domain request for the Workspace answers 503
    # projection_sync_unavailable.
    env = _compose_env()
    assert env.get("MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL") == "http://web:4002"
    assert env.get("MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN")
    assert (
        env.get("PROJECTION_SYNC_SERVICE_TOKEN")
        == env["MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN"]
    )
    # One source for the pair: a per-service override in a Compose file
    # would silently split it again.
    for compose_file in ("docker-compose.yml", "docker-compose.golden-path.yml"):
        compose = (REPO / compose_file).read_text(encoding="utf-8")
        assert "PROJECTION_SYNC_SERVICE_TOKEN" not in compose, compose_file


def test_golden_path_tests_the_containers_it_attested() -> None:
    # `docker compose run` also starts the target's dependencies, and here it
    # recreated the containers `Compose up` had started, validated and
    # attested (every dependency built from source, in runs 36512018617 and
    # 36517751730). That re-ran init-domain's release against the live stack;
    # in run 36517751730 the Worker was refused `operation_records` mid-release
    # and exited before Step 0.1. Only the Playwright container may start.
    for workflow in GOLDEN_PATH_WORKFLOWS:
        job = _golden_path_job(workflow)
        step = job.split("- name: Run golden-path test", 1)[1]
        step = step.split("\n      - name:", 1)[0]
        assert "run --rm --no-deps" in step, workflow

    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("\ngolden-path-run:\n", 1)[1].split("\n\n", 1)[0]
    assert "run --rm --no-deps" in target
