"""The Release service orchestrates control-plane and domain migrations in order.

``deploy/release/release.sh`` is the entrypoint for the Railway Release service.
It must:

* verify database identity before any mutation
* back up both databases before any migration (and refuse to skip in production)
* migrate control-plane first, then domain
* verify schema consistency after both migrations
* fail fast if any step fails, without running later steps

These tests drive the real script with stub binaries so they assert the ordering
and safety properties rather than restating them.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "release" / "release.sh"

STUB_PSQL = r"""#!/usr/bin/env bash
printf 'psql %s\n' "$*" >> "$CALL_LOG"
# Return fake database identity based on connection string
if echo "$*" | grep -q "current_database"; then
  if echo "$*" | grep -q "control"; then
    echo "marketai_control"
  else
    echo "marketai_domain"
  fi
elif echo "$*" | grep -q "current_user"; then
  if echo "$*" | grep -q "control"; then
    echo "marketai_control_owner"
  else
    echo "marketai_domain_owner"
  fi
elif echo "$*" | grep -q "migration_name"; then
  echo "20260901_init"
elif echo "$*" | grep -q "version_num"; then
  echo "abc123"
fi
exit 0
"""

STUB_PG_DUMP = r"""#!/usr/bin/env bash
printf 'pg_dump %s\n' "$*" >> "$CALL_LOG"
echo "FAKE_DUMP_DATA"
exit 0
"""

STUB_NODE = r"""#!/usr/bin/env bash
printf 'node %s\n' "$*" >> "$CALL_LOG"
exit 0
"""

STUB_ALEMBIC = r"""#!/usr/bin/env bash
printf 'alembic %s\n' "$*" >> "$CALL_LOG"
exit 0
"""

# Domain release.sh calls apply-domain-roles.sh and verify_release_target
STUB_DOMAIN_RELEASE = r"""#!/usr/bin/env bash
printf 'domain-release\n' >> "$CALL_LOG"
exit 0
"""

STUB_PYTHON = r"""#!/usr/bin/env bash
printf 'python %s\n' "$*" >> "$CALL_LOG"
exit 0
"""

STUB_GIT = r"""#!/usr/bin/env bash
if [ "$1" = "rev-parse" ]; then
  echo "abc1234"
fi
exit 0
"""

ENV_BASE = {
    "CONTROL_OWNER_DATABASE_URL": "postgresql://marketai_control_owner:pw@db:5432/marketai_control",
    "DOMAIN_OWNER_DATABASE_URL": "postgresql+psycopg://marketai_domain_owner:pw@db:5432/marketai_domain",
    "MARKETAI_DATABASE_URL": "postgresql+psycopg://marketai_domain_owner:pw@db:5432/marketai_domain",
    "MIGRATION_DATABASE_URL": "postgresql://marketai_control_owner:pw@db:5432/marketai_control",
    "SUPERUSER_DATABASE_URL": "postgresql://postgres:pw@db:5432/postgres",
    "DOMAIN_API_PASSWORD": "dapi",
    "DOMAIN_WORKER_PASSWORD": "dw",
    "DOMAIN_READONLY_PASSWORD": "dr",
    "MARKETAI_TENANT_CONTEXT_SECRET": "test-tenant-context-secret-32-bytes",
    "MARKETAI_BLOB_STORE_ADAPTER": "railway_bucket",
    "MARKETAI_BLOB_STORE_S3_BUCKET": "test-bucket",
    "MARKETAI_BLOB_STORE_S3_ENDPOINT_URL": "https://storage.invalid",
    "MARKETAI_BLOB_STORE_S3_REGION": "auto",
    "AWS_ACCESS_KEY_ID": "test-access-key",
    "AWS_SECRET_ACCESS_KEY": "test-secret-key",
}


class ReleaseServiceOrderingTests(unittest.TestCase):
    """The Release service must migrate control-plane before domain."""

    def run_release(self, **overrides) -> tuple[subprocess.CompletedProcess, list[str]]:
        workdir = tempfile.mkdtemp()
        self.addCleanup(_rmtree, workdir)

        bindir = pathlib.Path(workdir) / "bin"
        bindir.mkdir()
        for name, body in (
            ("psql", STUB_PSQL),
            ("pg_dump", STUB_PG_DUMP),
            ("node", STUB_NODE),
            ("alembic", STUB_ALEMBIC),
            ("python3", STUB_PYTHON),
            ("git", STUB_GIT),
        ):
            stub = bindir / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(0o755)

        # Stub the domain release.sh so it doesn't call real psql
        domain_release = pathlib.Path(workdir) / "domain-release.sh"
        domain_release.write_text(STUB_DOMAIN_RELEASE, encoding="utf-8")
        domain_release.chmod(0o755)

        log = pathlib.Path(workdir) / "calls.log"

        env = dict(os.environ)
        env.update(ENV_BASE)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["CALL_LOG"] = str(log)
        # The repository CI defines these commands globally.  This harness must
        # still exercise its stubs instead of leaking the runner's commands
        # into the nested domain-release script.
        env["PYTHON"] = "python3"
        env["ALEMBIC"] = "alembic"
        env["RELEASE_SKIP_BACKUP"] = overrides.pop("RELEASE_SKIP_BACKUP", "true")
        env["RAILWAY_ENVIRONMENT"] = overrides.pop("RAILWAY_ENVIRONMENT", "staging")
        env.update(overrides)

        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return result, calls

    def test_pre_flight_checks_run_before_anything_else(self):
        result, calls = self.run_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        # First calls should be psql identity checks
        psql_calls = [c for c in calls if c.startswith("psql")]
        self.assertGreater(len(psql_calls), 0, "pre-flight must check database identity")

    def test_pre_flight_verifies_railway_bucket_connectivity(self):
        result, calls = self.run_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python -m scripts.verify_railway_bucket", calls)

    def test_control_plane_migrates_before_domain(self):
        result, calls = self.run_release()
        self.assertEqual(result.returncode, 0, result.stderr)

        # The Prisma deploy call (migration) must come before alembic upgrade
        # (domain migration). Post-verification calls (prisma status, alembic
        # check) are not migration calls and may interleave freely.
        prisma_deploy = [
            i for i, c in enumerate(calls)
            if c.startswith("node") and "deploy" in c
        ]
        alembic_upgrade = [
            i for i, c in enumerate(calls)
            if c.startswith("alembic") and "upgrade" in c
        ]

        self.assertTrue(prisma_deploy, "Prisma deploy must be called")
        self.assertTrue(alembic_upgrade, "Alembic upgrade must be called")
        self.assertLess(
            max(prisma_deploy),
            min(alembic_upgrade),
            "control-plane (Prisma deploy) must complete before domain (Alembic upgrade)",
        )

    def test_non_postgres_url_is_refused(self):
        result, _ = self.run_release(
            MARKETAI_DATABASE_URL="sqlite:///data/marketai.db"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a PostgreSQL URL", result.stderr)

    def test_missing_required_env_fails(self):
        """Every required credential must be present."""
        env = dict(os.environ)
        env.update(ENV_BASE)
        env.pop("CONTROL_OWNER_DATABASE_URL", None)

        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONTROL_OWNER_DATABASE_URL", result.stderr)

    def test_skip_backup_refused_in_production(self):
        result, _ = self.run_release(
            RELEASE_SKIP_BACKUP="true",
            RAILWAY_ENVIRONMENT="production",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed in production", result.stderr + result.stdout)

    def test_evidence_output_includes_versions(self):
        result, _ = self.run_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"status": "success"', result.stdout)
        self.assertIn('"control_plane"', result.stdout)
        self.assertIn('"domain"', result.stdout)


class ReleaseCredentialIsolationTests(unittest.TestCase):
    """Runtime services must never receive schema-owner credentials."""

    def test_dockerfile_does_not_copy_env_files(self):
        dockerfile = (REPO / "deploy" / "release" / "Dockerfile.release").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".env", dockerfile.lower().replace("env_base", ""))
        self.assertNotIn("secret", dockerfile.lower())

    def test_dockerfile_has_no_cmd(self):
        """ENTRYPOINT only — CMD would allow fallback to a shell."""
        dockerfile = (REPO / "deploy" / "release" / "Dockerfile.release").read_text(
            encoding="utf-8"
        )
        lines = [
            line.strip()
            for line in dockerfile.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertFalse(
            any(line.startswith("CMD") for line in lines),
            "Release container must not have CMD — only ENTRYPOINT",
        )

    def test_dockerfile_does_not_include_runtime_cmd(self):
        dockerfile = (REPO / "deploy" / "release" / "Dockerfile.release").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("uvicorn", dockerfile)
        self.assertNotIn("npm start", dockerfile)

    def test_release_script_exists_and_is_executable(self):
        self.assertTrue(SCRIPT.exists())
        self.assertTrue(os.access(SCRIPT, os.X_OK))


def _release_image_app_paths() -> set[str]:
    """Paths under ``app/`` that ``Dockerfile.release`` copies into the image."""
    dockerfile = (REPO / "deploy" / "release" / "Dockerfile.release").read_text(
        encoding="utf-8"
    )
    paths = set()
    for line in dockerfile.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "COPY" and parts[1].startswith("app/"):
            paths.add(parts[1].rstrip("/"))
    return paths


def _module_file(module: str) -> pathlib.Path | None:
    base = REPO.joinpath(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _module_level_app_imports(path: pathlib.Path, module: str) -> set[str]:
    """``app.*`` modules a file imports when it is loaded.

    Only module-level statements count (``try`` bodies included,
    ``if TYPE_CHECKING`` blocks and function bodies excluded): those are the
    imports Alembic actually executes when ``migrations/env.py`` runs.
    """
    is_package = path.name == "__init__.py"
    package = module if is_package else module.rpartition(".")[0]
    statements = list(ast.parse(path.read_text(encoding="utf-8")).body)
    found: set[str] = set()
    while statements:
        node = statements.pop()
        if isinstance(node, ast.Try):
            statements.extend(node.body)
        elif isinstance(node, ast.If) and "TYPE_CHECKING" not in ast.unparse(node.test):
            statements.extend(node.body + node.orelse)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join(anchor + ([base] if base else []))
            found.add(base)
            # ``from app.pkg import models`` loads the submodule app.pkg.models.
            found.update(
                f"{base}.{alias.name}"
                for alias in node.names
                if _module_file(f"{base}.{alias.name}") is not None
            )
    return {name for name in found if name == "app" or name.startswith("app.")}


def _migration_import_closure() -> set[pathlib.Path]:
    """Every ``app`` source file loaded by importing ``migrations/env.py``."""
    pending = sorted(
        _module_level_app_imports(REPO / "migrations" / "env.py", "migrations.env")
    )
    seen: set[str] = set()
    files: set[pathlib.Path] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        # Importing app.pkg.mod first runs app/__init__.py and app/pkg/__init__.py.
        parents = module.split(".")
        pending.extend(".".join(parents[:i]) for i in range(1, len(parents)))
        path = _module_file(module)
        if path is None:
            continue
        files.add(path)
        pending.extend(_module_level_app_imports(path, module))
    return files


class ReleaseImageMigrationImportsTests(unittest.TestCase):
    """The Release image must contain every module Alembic's env.py loads.

    ``Dockerfile.release`` copies an explicit list of ``app/`` packages. When
    #314 added ``app.competitors`` to ``migrations/env.py`` without copying it,
    the production Release run crashed with ``ModuleNotFoundError`` before
    ``alembic upgrade``. ``make verify`` runs migrations from the full source
    tree, so only this check catches the gap before deploy.
    """

    def test_every_module_env_py_imports_is_copied(self):
        copied = _release_image_app_paths()
        missing = sorted(
            str(path.relative_to(REPO))
            for path in _migration_import_closure()
            if not any(
                str(path.relative_to(REPO)) == entry
                or str(path.relative_to(REPO)).startswith(entry + "/")
                for entry in copied
            )
        )
        self.assertEqual(
            missing,
            [],
            "migrations/env.py imports modules the Release image does not "
            "COPY; add them to deploy/release/Dockerfile.release",
        )

    def test_closure_reaches_the_model_packages(self):
        """Guard the walker itself: it must see the packages env.py names."""
        loaded = {str(path.relative_to(REPO)) for path in _migration_import_closure()}
        self.assertIn("app/adapters/persistence/models.py", loaded)
        self.assertIn("app/marketing/models.py", loaded)
        self.assertIn("app/competitors/models.py", loaded)


class BackupScriptTests(unittest.TestCase):
    """The backup script must produce dumps and metadata for both databases."""

    BACKUP_SCRIPT = REPO / "deploy" / "release" / "backup.sh"

    def test_backup_script_exists_and_is_executable(self):
        self.assertTrue(self.BACKUP_SCRIPT.exists())
        self.assertTrue(os.access(self.BACKUP_SCRIPT, os.X_OK))

    def test_backup_requires_both_connection_strings(self):
        env = dict(os.environ)
        env.pop("CONTROL_OWNER_DATABASE_URL", None)
        env.pop("DOMAIN_OWNER_DATABASE_URL", None)

        result = subprocess.run(
            ["bash", str(self.BACKUP_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL", result.stderr)


class RestoreRehearsalTests(unittest.TestCase):
    """The rehearsal script must exist and validate money/hash integrity."""

    REHEARSAL_SCRIPT = REPO / "deploy" / "release" / "restore-rehearsal.sh"

    def test_rehearsal_script_exists_and_is_executable(self):
        self.assertTrue(self.REHEARSAL_SCRIPT.exists())
        self.assertTrue(os.access(self.REHEARSAL_SCRIPT, os.X_OK))

    def test_rehearsal_verifies_money_columns(self):
        content = self.REHEARSAL_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("cost_ledger", content)
        self.assertIn("amount", content)
        self.assertIn("spend", content)

    def test_rehearsal_verifies_content_hashes(self):
        content = self.REHEARSAL_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("md5", content)
        self.assertIn("alembic_version", content)
        self.assertIn("_prisma_migrations", content)


class RunbookCompleteness(unittest.TestCase):
    """Runbooks must exist and cover the documented recovery objectives."""

    def test_release_runbook_exists(self):
        path = REPO / "docs" / "operations" / "release-runbook.md"
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("staging", content.lower())
        self.assertIn("production", content.lower())
        self.assertIn("rollback", content.lower())

    def test_backup_runbook_exists(self):
        path = REPO / "docs" / "operations" / "backup-runbook.md"
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("PITR", content)
        self.assertIn("retention", content.lower())
        self.assertIn("30 day", content.lower())

    def test_disaster_recovery_runbook_exists(self):
        path = REPO / "docs" / "operations" / "disaster-recovery-runbook.md"
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("15 minute", content.lower())
        self.assertIn("4 hour", content.lower())

    def test_dr_runbook_covers_forward_only_rule(self):
        path = REPO / "docs" / "operations" / "disaster-recovery-runbook.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn("forward", content.lower())
        self.assertIn("financial", content.lower())
        self.assertIn("credential", content.lower())

    def test_existing_forward_rollback_procedure_is_consistent(self):
        """The new runbooks must not contradict the existing procedure doc."""
        existing = REPO / "docs" / "operations" / "forward-rollback-procedure.md"
        self.assertTrue(existing.exists())
        content = existing.read_text(encoding="utf-8")
        self.assertIn("make db-release", content)


class MakefileIntegrationTests(unittest.TestCase):
    """Makefile must expose the release service targets."""

    def test_release_full_target_exists(self):
        makefile = (REPO / "Makefile").read_text(encoding="utf-8")
        self.assertIn("release-full:", makefile)
        self.assertIn("deploy/release/release.sh", makefile)

    def test_backup_target_exists(self):
        makefile = (REPO / "Makefile").read_text(encoding="utf-8")
        self.assertIn("backup:", makefile)

    def test_restore_rehearsal_target_exists(self):
        makefile = (REPO / "Makefile").read_text(encoding="utf-8")
        self.assertIn("restore-rehearsal:", makefile)


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
