"""Regression checks for the production Railway Web/API configuration seam."""

from __future__ import annotations

import pathlib
import os
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = ROOT / "deploy" / "railway" / "env"


def contract_entries(service: str) -> dict[str, str]:
    entries = {}
    for line in (ENV / f"{service}.env.contract").read_text().splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) == 2:
            entries[parts[1]] = parts[0]
    return entries


class RailwayDeploymentContractTests(unittest.TestCase):
    def test_web_contract_uses_runtime_variable_names(self) -> None:
        entries = contract_entries("web")
        for name in (
            "DOMAIN_API_BASE_URL",
            "DELEGATED_SIGNING_KEY_ENCRYPTION_KEY",
            "PROJECTION_SYNC_SERVICE_TOKEN",
            "DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN",
            "MFA_ENCRYPTION_KEY",
            "NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS",
        ):
            self.assertEqual(entries.get(name), "REQUIRED", name)
        self.assertNotIn("DOMAIN_API_INTERNAL_URL", entries)
        self.assertNotIn("DELEGATED_SIGNING_PRIVATE_KEY", entries)
        self.assertNotIn("DELEGATED_SIGNING_KEY_ID", entries)

    def test_web_image_inlines_storage_origins_before_next_build(self) -> None:
        dockerfile = (ROOT / "deploy" / "railway" / "Dockerfile.web").read_text()
        arg = dockerfile.index('ARG NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS=""')
        env = dockerfile.index(
            "ENV NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS=${NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS}"
        )
        build = dockerfile.index("npm run build")
        self.assertLess(arg, env)
        self.assertLess(env, build)

    def test_release_template_converges_browser_cors_from_the_web_origin(self) -> None:
        template = (ENV / "release.env.template").read_text()
        self.assertIn("MARKETAI_BLOB_STORE_BROWSER_ORIGINS=${{Web.APP_URL}}", template)
        release = (ROOT / "deploy" / "release" / "release.sh").read_text()
        self.assertIn("MARKETAI_BLOB_STORE_BROWSER_ORIGINS", release)

    def test_api_contract_requires_signed_entitlement_verification(self) -> None:
        entries = contract_entries("api")
        self.assertEqual(entries.get("ENTITLEMENT_VERIFICATION_KEY_RING"), "REQUIRED")
        self.assertEqual(
            entries.get("MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL"), "REQUIRED"
        )
        self.assertNotIn("CONTROL_PLANE_INTERNAL_BASE_URL", entries)

    def test_worker_contract_requires_onboarding_delivery_settings(self) -> None:
        # The Worker is the only process that delivers onboarding facts, and it
        # skips delivery without an error when either setting is empty.
        entries = contract_entries("worker")
        self.assertEqual(
            entries.get("MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL"), "REQUIRED"
        )
        self.assertEqual(entries.get("DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN"), "REQUIRED")
        self.assertEqual(entries.get("MARKETAI_PROJECTION_SYNC_TIMEOUT_MS"), "OPTIONAL")
        self.assertNotIn("CONTROL_PLANE_INTERNAL_BASE_URL", entries)

    def test_worker_template_references_the_api_base_url_and_web_token(self) -> None:
        template = (ENV / "worker.env.template").read_text()
        self.assertIn(
            "MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL="
            "${{marketai-api.MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL}}",
            template,
        )
        self.assertIn(
            "DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN="
            "${{marketai-web.DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN}}",
            template,
        )

    def test_entitlement_dispatcher_has_separate_private_runtime_contract(self) -> None:
        entries = contract_entries("dispatcher")
        for name in (
            "DATABASE_URL",
            "DOMAIN_API_BASE_URL",
            "CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN",
            "ENTITLEMENT_SNAPSHOT_SIGNING_KEY",
            "ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID",
        ):
            self.assertEqual(entries.get(name), "REQUIRED", name)
        for name in (
            "MARKETAI_DATABASE_URL",
            "NEXTAUTH_SECRET",
            "DELEGATED_SIGNING_KEY_ENCRYPTION_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "META_WRITE_ACCESS_TOKEN",
        ):
            self.assertEqual(entries.get(name), "DENIED", name)

    def test_static_validator_rejects_contract_runtime_drift(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "deploy/railway/validate-static.sh"), "--skip-docker-build"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Runtime variable names", result.stdout)
        for line in (
            "worker requires runtime variable MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL",
            "worker requires runtime variable DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN",
            "worker contract excludes obsolete variable CONTROL_PLANE_INTERNAL_BASE_URL",
        ):
            self.assertIn(line, result.stdout)

    def test_live_validator_checks_login_page_not_redirecting_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            curl = pathlib.Path(directory) / "curl"
            curl.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'/auth/login'*) printf 'HTTP/2 200\\r\\nContent-Security-Policy: default-src self\\r\\nX-Frame-Options: DENY\\r\\n\\r\\n' ;;\n"
                "  *'/api/readiness'*|*'/.well-known/jwks.json'*) printf '200' ;;\n"
                "  *) printf 'HTTP/2 307\\r\\nLocation: /auth/login\\r\\n\\r\\n' ;;\n"
                "esac\n"
            )
            curl.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{directory}:{env['PATH']}"
            env["RAILWAY_WEB_URL"] = "https://example.invalid"
            result = subprocess.run(
                ["bash", str(ROOT / "deploy/railway/validate-live.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Public login page returns HTTP 200", result.stdout)
        self.assertIn("Web sends Content-Security-Policy", result.stdout)


if __name__ == "__main__":
    unittest.main()
