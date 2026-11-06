"""The control plane must not become a second MarketAI domain backend.

These checks run without PostgreSQL or Node and are part of ``make verify``.
They assert the repository-level boundary that ADR 0001 and ADR 0003 define:
the Prisma schema owns identity and Workspace membership only, and nothing in
the Python domain service reaches into control-plane persistence.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = REPO_ROOT / "web"


def _strip_ts_comments(source: str) -> str:
    """Comments explain what was removed; assertions run against code only."""

    source = re.sub(r"/\*[\s\S]*?\*/", "", source)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def _schema_declarations() -> str:
    body = (WEB / "prisma" / "schema.prisma").read_text(encoding="utf-8")
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//")
    )


class ControlPlaneBoundaryTests(unittest.TestCase):
    def test_web_service_exists_with_a_pinned_upstream(self):
        pin = json.loads((WEB / "UPSTREAM.json").read_text(encoding="utf-8"))

        self.assertEqual(
            pin["upstream"]["repository"],
            "https://github.com/boxyhq/saas-starter-kit",
        )
        self.assertRegex(pin["upstream"]["pinnedCommit"], r"^[0-9a-f]{40}$")
        self.assertFalse(pin["updatePolicy"]["automaticRebase"])
        self.assertFalse(pin["updatePolicy"]["automaticDependencyUpdates"])

    def test_apache_notices_are_retained(self):
        licence = (WEB / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", licence)
        self.assertIn("Version 2.0", licence)

        notice = (WEB / "NOTICE").read_text(encoding="utf-8")
        pin = json.loads((WEB / "UPSTREAM.json").read_text(encoding="utf-8"))
        self.assertIn(pin["upstream"]["pinnedCommit"], notice)

    def test_control_plane_schema_declares_no_domain_table(self):
        declarations = _schema_declarations()

        models = set(re.findall(r"^model\s+(\w+)", declarations, re.M))

        self.assertEqual(
            models,
            {
                "Account",
                "Session",
                "VerificationToken",
                "User",
                "Team",
                "TeamMember",
                "Invitation",
                "PasswordReset",
                # The BFF's own ES256 signing keys for delegated JWTs (spec
                # §1.6) — control-plane identity material, not domain data:
                # FastAPI only ever sees the public half via
                # GET /.well-known/jwks.json.
                "DelegatedSigningKey",
                # Entitlement/subscription models live in the control plane
                # because they own the commercial relationship. The domain
                # service only sees signed projections via the inbox.
                "Subscription",
                "PlanVersion",
                "AccessGrant",
                "Restriction",
                "EntitlementSnapshot",
                "EntitlementSnapshotOutbox",
                "CommercialAccessBinding",
                "CommercialAccessProjection",
                "ControlPlaneAuditEvent",
                # Revocable authentication, audited recovery and onboarding
                # projections are control-plane concerns. `StepUpGrant` is
                # no longer written (ADR 0015) but its table stays.
                "MfaCredential",
                "MfaRecoveryCode",
                "MfaResetCase",
                "MfaResetApproval",
                "MfaRecoveryEmailChallenge",
                # Distinct-subject dual-control cases for exceptional support
                # actions (invite reissue and provisioning reconciliation).
                "OperatorActionCase",
                "OperatorActionApproval",
                "StepUpGrant",
                "OnboardingProgress",
                "OnboardingDomainFact",
                "ControlPlaneOutboxEvent",
            },
        )

    def test_control_plane_role_enum_is_owner_and_member_only(self):
        declarations = _schema_declarations()
        enum_body = re.search(r"enum Role \{(.*?)\}", declarations, re.S)

        self.assertIsNotNone(enum_body)
        values = enum_body.group(1).split()
        self.assertEqual(sorted(values), ["MEMBER", "OWNER"])

    def test_release_path_never_pushes_the_schema(self):
        package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))

        for name, command in package["scripts"].items():
            self.assertNotIn("db push", command, f"script {name} uses prisma db push")

        # Migrations run as the schema owner (MIGRATION_DATABASE_URL), never as
        # the runtime role in DATABASE_URL, which holds no DDL privileges.
        for script in ("db:migrate:dev", "db:migrate:deploy", "db:migrate:status"):
            self.assertIn(
                "scripts/prisma-migrate.mjs",
                package["scripts"][script],
                f"{script} must run through the migration-identity wrapper",
            )

    def test_invitations_are_single_use_and_email_addressed(self):
        declarations = _schema_declarations()
        model = declarations.split("model Invitation {")[1].split("}")[0]

        # Upstream link invitations: a nullable email plus these two columns.
        self.assertNotIn("sentViaEmail", model)
        self.assertNotIn("allowedDomains", model)
        self.assertRegex(model, r"(?m)^\s*email\s+String\s*$")
        self.assertNotRegex(model, r"(?m)^\s*email\s+String\?")

    def test_control_plane_documents_both_database_identities(self):
        env_example = (WEB / ".env.example").read_text(encoding="utf-8")
        env_migrate = (WEB / ".env.migrate.example").read_text(encoding="utf-8")

        self.assertRegex(
            env_example, r"(?m)^DATABASE_URL=postgresql://marketai_control_app:"
        )
        self.assertRegex(
            env_migrate,
            r"(?m)^MIGRATION_DATABASE_URL=postgresql://marketai_control_owner:",
        )

        # `web/.env` becomes the running process's environment, so the
        # schema-owner credential must not be templated into it.
        self.assertNotIn("MIGRATION_DATABASE_URL=", env_example)
        self.assertNotIn("marketai_control_owner", env_example)

    def test_sessions_are_revocable_server_side(self):
        next_auth = _strip_ts_comments(
            (WEB / "lib" / "nextAuth.ts").read_text(encoding="utf-8")
        )
        middleware = _strip_ts_comments(
            (WEB / "middleware.ts").read_text(encoding="utf-8")
        )

        # The JWT is a carrier bound to a server-side session row.
        self.assertIn("createAuthSession", next_auth)
        self.assertIn("findActiveAuthSession", next_auth)

        # The middleware must not authorize on the signature alone.
        self.assertNotIn("getToken", middleware)
        self.assertIn("/api/auth/session", middleware)

    def test_python_service_does_not_read_the_control_plane_datastore(self):
        offenders = []

        for path in list((REPO_ROOT / "app").rglob("*.py")) + list(
            (REPO_ROOT / "worker").rglob("*.py")
        ):
            body = path.read_text(encoding="utf-8")
            for needle in ("prisma", "CONTROL_PLANE_DATABASE_URL", "TeamMember"):
                if needle in body:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")

        self.assertEqual(offenders, [])

    def test_control_plane_does_not_hold_domain_database_configuration(self):
        env_example = (WEB / ".env.example").read_text(encoding="utf-8")

        # The control plane talks to FastAPI over HTTP, never to its database.
        self.assertNotIn("DOMAIN_DATABASE_URL", env_example)
        self.assertNotIn("MARKETAI_DATABASE_URL", env_example)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
