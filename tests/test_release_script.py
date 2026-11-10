"""`make db-release` is the only supported way to release the domain database.

Roles have to be converged before the RLS migration runs: the role file revokes
the former shared runtime exemption and declares the explicit API/Worker grant
matrix. Documentation and CI step order are not enough -- an operator running
`alembic upgrade head` alone can leave an old BYPASSRLS bit alive or leave new
tables inaccessible to both runtime roles.

Two properties beyond the ordering:

* a domain release converges the *domain* roles only. `apply-roles.sh`
  bootstraps both deployment units and needs the control-plane passwords to do
  it, so routing releases through it would re-set `marketai_control_owner`'s
  password on every domain deploy -- and fail, or silently reapply a stale
  secret, when a control-plane credential is rotated elsewhere.
* the target is verified over the connection Alembic will use. Converging roles
  over the superuser connection and migrating over `MARKETAI_DATABASE_URL`
  means two independent strings; checking the first proves nothing about the
  second.

These tests drive the real script with stub `psql`, `python` and `alembic`
binaries, so they assert the ordering and the refusals rather than restating
them.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "postgres" / "release.sh"

#: Only the domain half. The control-plane passwords are deliberately absent:
#: a domain release that needs them is a domain release coupled to the other
#: deployment unit's secrets.
DOMAIN_PASSWORDS = {
    "DOMAIN_API_PASSWORD": "dapi",
    "DOMAIN_WORKER_PASSWORD": "dw",
    "DOMAIN_READONLY_PASSWORD": "dr",
    "MARKETAI_TENANT_CONTEXT_SECRET": "test-tenant-context-secret-32-bytes",
}

CONTROL_PASSWORDS = {
    "CONTROL_OWNER_PASSWORD": "co",
    "CONTROL_APP_PASSWORD": "ca",
    "DOMAIN_OWNER_PASSWORD": "do",
    "DOMAIN_APP_PASSWORD": "da",
}

OWNER_URL = "postgresql+psycopg://marketai_domain_owner:pw@db:5432/marketai_domain"

STUB_PSQL = """#!/usr/bin/env bash
printf 'psql %s\\n' "$*" >> "$CALL_LOG"
exit 0
"""

# Stands in for `python -m scripts.verify_release_target`, whose own behaviour
# is asserted against a real cluster in tests/test_domain_app_transition.py.
STUB_PYTHON = """#!/usr/bin/env bash
printf 'python %s nonce=%s\\n' "$*" "$MARKETAI_RELEASE_NONCE" >> "$CALL_LOG"
exit "${STUB_VERIFY_EXIT:-0}"
"""

STUB_ALEMBIC = """#!/usr/bin/env bash
printf 'alembic %s\\n' "$*" >> "$CALL_LOG"
exit 0
"""


class ReleaseOrderingTests(unittest.TestCase):
    def run_release(self, *, control_passwords: bool = False, **overrides):
        workdir = tempfile.mkdtemp()
        self.addCleanup(_rmtree, workdir)

        bindir = pathlib.Path(workdir) / "bin"
        bindir.mkdir()
        for name, body in (
            ("psql", STUB_PSQL),
            ("python3", STUB_PYTHON),
            ("alembic", STUB_ALEMBIC),
        ):
            stub = bindir / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(0o755)

        log = pathlib.Path(workdir) / "calls.log"

        env = dict(os.environ)
        env.pop("SUPERUSER_DATABASE_URL", None)
        env.pop("ALEMBIC", None)
        env.pop("PYTHON", None)
        for name in CONTROL_PASSWORDS:
            env.pop(name, None)
        env.update(DOMAIN_PASSWORDS)
        if control_passwords:
            env.update(CONTROL_PASSWORDS)
        env["MARKETAI_DATABASE_URL"] = OWNER_URL
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["CALL_LOG"] = str(log)
        env.update(overrides)

        result = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return result, calls

    def test_roles_are_converged_and_the_target_checked_before_migrating(self):
        result, calls = self.run_release()

        self.assertEqual(result.returncode, 0, result.stderr)

        kinds = [c.split(" ", 1)[0] for c in calls]
        self.assertEqual(
            kinds,
            ["psql", "python", "alembic", "psql"],
            "roles, target check, migration, then explicit grants for new tables",
        )

        migrations = [c for c in calls if c.startswith("alembic")]
        self.assertEqual(migrations, ["alembic upgrade head"])

    def test_a_target_that_fails_verification_is_not_migrated(self):
        # The verifier refuses when the database, the role or the bridge state
        # on the migration target is not what this release prepared.
        result, calls = self.run_release(STUB_VERIFY_EXIT="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(
            "alembic upgrade head",
            calls,
            "refusing must mean not migrating, not warning and continuing",
        )

    def test_the_same_nonce_is_stamped_and_demanded(self):
        # Generated once per release: step 1 stamps it on the database, step 2
        # refuses any database not carrying it. Two values would mean the check
        # can never pass; none would mean it can never fail.
        _, calls = self.run_release()

        stamped = [c for c in calls if "release_nonce=" in c]
        self.assertEqual(len(stamped), 2, calls)

        nonce = stamped[0].split("release_nonce=", 1)[1].split()[0]
        self.assertRegex(nonce, r"^[0-9a-f]{32}$")
        self.assertIn(f"release_nonce={nonce}", stamped[1])

        demanded = [c for c in calls if c.startswith("python")]
        self.assertEqual(len(demanded), 1)
        self.assertIn(nonce, demanded[0], "the verifier was handed a different nonce")

    def test_the_target_is_checked_over_the_migration_connection(self):
        # Not over the superuser connection the roles were converged with: the
        # two are independent strings and may name different clusters.
        _, calls = self.run_release()

        verification = [c for c in calls if c.startswith("python")]
        self.assertEqual(len(verification), 1)
        self.assertTrue(
            verification[0].startswith("python -m scripts.verify_release_target"),
            verification,
        )

    def test_a_domain_release_does_not_touch_the_control_plane(self):
        # No control-plane passwords in the environment at all, and it still
        # succeeds; the control-plane provisioning file is never applied.
        result, calls = self.run_release()

        self.assertEqual(result.returncode, 0, result.stderr)

        applied = " ".join(c for c in calls if c.startswith("psql"))
        self.assertIn("domain-tenant-roles.sql", applied)
        self.assertNotIn(
            "control-plane-domain-roles.sql",
            applied,
            "rolling the domain schema must not re-set control-plane credentials",
        )

    def test_a_non_postgres_url_is_refused_before_anything_runs(self):
        result, calls = self.run_release(
            MARKETAI_DATABASE_URL="sqlite:///data/marketai.db"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_the_owner_url_is_required(self):
        env = dict(os.environ)
        env.pop("MARKETAI_DATABASE_URL", None)
        env.update(DOMAIN_PASSWORDS)

        result = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MARKETAI_DATABASE_URL", result.stderr)

    def test_multi_word_interpreter_commands_are_honoured(self):
        # CI pins them with PYTHON/ALEMBIC="python -m ...". Quoting either as
        # one word makes the release fail after the roles have been changed.
        _, calls = self.run_release(
            PYTHON="env MARKETAI_RELEASE_PROBE=1 python3",
            ALEMBIC="env MARKETAI_RELEASE_PROBE=1 alembic",
        )

        self.assertTrue(
            any(c.startswith("python -m scripts.verify_release_target") for c in calls),
            calls,
        )
        self.assertIn("alembic upgrade head", calls)


class ReleaseEntryPointTests(unittest.TestCase):
    def test_make_exposes_the_release_path(self):
        makefile = (REPO / "Makefile").read_text(encoding="utf-8")

        self.assertIn("db-release:", makefile)
        self.assertIn("deploy/postgres/release.sh", makefile)

    def test_the_release_sql_never_names_the_control_plane_database(self):
        # `domain-tenant-roles.sql` runs on every release. A statement naming
        # marketai_control would rewrite the other unit's ACLs on every deploy,
        # and would fail outright on a domain-only cluster where that database
        # does not exist. The cross-database denials belong to bootstrap.
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )

        executable = [
            line
            for line in sql.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]

        self.assertFalse(
            [line for line in executable if "marketai_control" in line],
            "a domain release must stay inside the domain database",
        )

    def test_the_privilege_reset_and_the_regrant_commit_together(self):
        # A release applies this file while FastAPI and the Worker serve. It
        # revokes every runtime role's table privileges and grants the matrix
        # back; unless both commit as one transaction, a live query in between
        # is refused (Golden Path PR Validation run 36517751730: the Worker
        # exited on `permission denied for table operation_records`).
        # tests/test_postgres_role_convergence.py proves it on PostgreSQL.
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        executable = [
            line.strip()
            for line in sql.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]

        connect = executable.index("\\connect -reuse-previous=on marketai_domain")
        self.assertEqual(executable[connect + 1], "BEGIN;")
        self.assertEqual(executable[-1], "COMMIT;")
        self.assertEqual(executable.count("BEGIN;"), 1)
        self.assertEqual(executable.count("COMMIT;"), 1)
        self.assertNotIn("ROLLBACK;", executable)

    def test_domain_role_replay_preserves_generation_draft_and_safe_credential_grants(self):
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )

        select_shared = sql.split("-- ---- SELECT: API + Readonly (shared) ----", 1)[1]
        select_shared = select_shared.split("-- ---- SELECT: API only", 1)[0]
        update_api = sql.split("-- ---- UPDATE: API ----", 1)[1]
        update_api = update_api.split("-- ---- INSERT: API ----", 1)[0]
        insert_api = sql.split("-- ---- INSERT: API ----", 1)[1]
        insert_api = insert_api.split("-- ---- SELECT: Worker ----", 1)[0]

        for section in (select_shared, update_api, insert_api):
            self.assertIn("'generation_drafts'", section)

        # The credential base tables are never granted whole to the API role:
        # the lifecycle commands and the BYOK readiness checks read named
        # non-secret columns, and the envelope columns stay Worker-only.
        versions_grant = sql.split(
            "ON TABLE public.credential_versions TO marketai_domain_api", 1
        )[0].rsplit("GRANT SELECT (", 1)[1]
        for column in (
            "id", "workspace_id", "credential_key", "version_no", "provider",
            "display_label", "state", "created_by", "created_at", "revoked_at",
        ):
            self.assertIn(column, versions_grant)
        for secret_column in ("encrypted_secret", "encrypted_data_key", "nonce"):
            self.assertNotIn(secret_column, versions_grant)
        bindings_grant = sql.split(
            "ON TABLE public.credential_bindings TO marketai_domain_api", 1
        )[0].rsplit("GRANT SELECT (", 1)[1]
        for column in ("binding_scope", "provider", "credential_version_id", "revision"):
            self.assertIn(column, bindings_grant)
        self.assertNotIn(
            "GRANT SELECT ON TABLE public.credential_versions TO marketai_domain_api",
            sql,
        )
        self.assertNotIn(
            "GRANT SELECT ON TABLE public.credential_bindings TO marketai_domain_api",
            sql,
        )

    def test_domain_role_replay_preserves_generation_vertical_slice_grants(self):
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        select_shared = sql.split("-- ---- SELECT: API + Readonly (shared) ----", 1)[1]
        select_shared = select_shared.split("-- ---- SELECT: API only", 1)[0]
        update_api = sql.split("-- ---- UPDATE: API ----", 1)[1]
        update_api = update_api.split("-- ---- INSERT: API ----", 1)[0]
        insert_api = sql.split("-- ---- INSERT: API ----", 1)[1]
        insert_api = insert_api.split("-- ---- SELECT: Worker ----", 1)[0]
        select_worker = sql.split("-- ---- SELECT: Worker ----", 1)[1]
        select_worker = select_worker.split("-- ---- INSERT: Worker ----", 1)[0]
        insert_worker = sql.split("-- ---- INSERT: Worker ----", 1)[1]
        insert_worker = insert_worker.split("-- ---- UPDATE: Worker ----", 1)[0]
        update_worker = sql.split("-- ---- UPDATE: Worker ----", 1)[1]
        update_worker = update_worker.split("-- Projection function", 1)[0]

        generation_tables = (
            "resolution_previews",
            "resolution_preview_confirmations",
            "generation_recipe_snapshots",
            "run_items",
            "provider_attempt_events",
            "provider_attempt_projections",
            "run_item_asset_version_bindings",
            "asset_qc_attempts",
            "asset_qc_check_results",
            "generation_run_commands",
            "export_packages",
        )
        for table in generation_tables:
            self.assertIn(f"'{table}'", select_shared)
            self.assertIn(f"'{table}'", select_worker)

        for table in (
            "resolution_previews",
            "resolution_preview_confirmations",
            "generation_recipe_snapshots",
            "run_items",
            "generation_run_commands",
            "export_packages",
        ):
            self.assertIn(f"'{table}'", insert_api)

        for table in (
            "resolution_previews",
            "run_items",
            "asset_versions",
        ):
            self.assertIn(f"'{table}'", update_api)

        # Append-only evidence is locked, never rewritten: the runtime roles
        # get a lock-only column grant instead of table-level UPDATE.
        lock_only = sql.split("-- ---- Row locks without write access ----", 1)[1]
        lock_only = lock_only.split("-- Projection function", 1)[0]
        for table in (
            "quality_reviews",
            "experiment_versions",
            "experiment_version_candidates",
            "truth_packages",
        ):
            self.assertNotIn(f"'{table}'", update_api)
            self.assertIn(f"'{table}'", lock_only)
        self.assertIn(
            "GRANT UPDATE (id) ON TABLE public.%I TO marketai_domain_api, "
            "marketai_domain_worker",
            lock_only,
        )
        self.assertIn(
            "GRANT UPDATE (id) ON TABLE public.experiments TO marketai_domain_worker",
            lock_only,
        )

        self.assertIn("'dimension_concept_keys'", select_shared)
        self.assertIn("'dimension_concept_keys'", insert_api)

        for table in (
            "assets",
            "spend_reservations",
            "run_item_asset_version_bindings",
            "asset_qc_attempts",
            "asset_qc_check_results",
        ):
            self.assertIn(f"'{table}'", insert_worker)

        for table in (
            "run_items",
            "asset_versions",
            "asset_qc_attempts",
            "export_packages",
        ):
            self.assertIn(f"'{table}'", update_worker)

    def test_domain_role_replay_preserves_scale_lineage_grants(self):
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        select_shared = sql.split("-- ---- SELECT: API + Readonly (shared) ----", 1)[1]
        select_shared = select_shared.split("-- ---- SELECT: API only", 1)[0]
        insert_api = sql.split("-- ---- INSERT: API ----", 1)[1]
        insert_api = insert_api.split("-- ---- SELECT: Worker ----", 1)[0]
        select_worker = sql.split("-- ---- SELECT: Worker ----", 1)[1]
        select_worker = select_worker.split("-- ---- INSERT: Worker ----", 1)[0]
        insert_worker = sql.split("-- ---- INSERT: Worker ----", 1)[1]
        insert_worker = insert_worker.split("-- ---- UPDATE: Worker ----", 1)[0]

        scale_tables = (
            "scale_delivery_targets",
            "scale_command_journal",
            "scale_lineage_guards",
            "scale_enrollment_candidates",
            "scale_delivery_evidence_snapshots",
            "scale_activation_previews",
        )
        for table in scale_tables:
            for section in (select_shared, insert_api, select_worker, insert_worker):
                self.assertIn(f"'{table}'", section)

    def test_domain_role_replay_preserves_scale_evidence_and_advice_grants(self):
        """The role replay repeats f5eeeab4e9b9's Scale grant matrix exactly,
        plus the Worker's INSERT on scale_episodes that zzb1e2f3a4b5 (#92)
        added to it.

        Every release revokes all table grants and re-grants only what the
        role file lists, so a Scale table the file forgets fails closed after
        the "4/4 runtime grants" pass even though its migration granted it.
        """
        import importlib.util
        import re

        migration = (
            REPO / "migrations" / "versions"
            / "f5eeeab4e9b9_scale_deterministic_evidence_and_advice.py"
        )
        spec = importlib.util.spec_from_file_location("scale_grant_matrix", migration)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        scale = sql.split("-- ---- Scale evidence, evaluation and advice", 1)[1]
        scale = scale.split("-- ---- Row locks without write access ----", 1)[0]

        def privileges(text: str) -> tuple[str, ...]:
            return tuple(part.strip() for part in text.split(",") if part.strip())

        replayed = {
            table: (privileges(api), privileges(worker))
            for table, api, worker in re.findall(
                r"ARRAY\['(\w+)', '([A-Z, ]*)', '([A-Z, ]*)'\]", scale
            )
        }
        # f5eeeab4e9b9 is history; the Worker's rollover INSERT came later.
        expected = dict(module.GRANT_MATRIX)
        api, worker = expected["scale_episodes"]
        self.assertNotIn("INSERT", worker)
        expected["scale_episodes"] = (api, ("INSERT",) + worker)
        self.assertEqual(replayed, expected)
        self.assertIn(
            "GRANT SELECT ON TABLE public.%I TO marketai_domain_api, "
            "marketai_domain_worker, marketai_domain_readonly",
            scale,
        )
        statements = "\n".join(
            line for line in scale.splitlines() if not line.strip().startswith("--")
        )
        self.assertNotIn("DELETE", statements)
        for table, columns in (
            ("scale_target_publication_previews", "consumed_at, result_target_version_id"),
            ("scale_target_withdrawal_previews", "consumed_at, withdrawn_at"),
        ):
            self.assertIn(f"'{table}'", scale)
            self.assertIn(
                f"GRANT UPDATE ({columns}) ON TABLE public.{table} TO marketai_domain_api",
                scale,
            )

    def test_domain_role_replay_lets_the_api_run_scale_enrollment_commands(self):
        """Enrollment, activation and Advice conversion are API commands.

        They lock the lineage guard and the Advice occurrence (SELECT ...
        FOR UPDATE needs UPDATE on one column), consume an Enrollment
        Candidate and an activation preview through consumed_at, and
        activation inserts the Episode's baseline delivery configuration.
        """
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        for statement in (
            "GRANT UPDATE (consumed_at) ON TABLE public.scale_enrollment_candidates "
            "TO marketai_domain_api",
            "GRANT UPDATE (consumed_at) ON TABLE public.scale_activation_previews "
            "TO marketai_domain_api",
            "GRANT INSERT ON TABLE public.scale_delivery_config_sources "
            "TO marketai_domain_api",
        ):
            self.assertIn(statement, sql)

        row_locks = sql.split("-- ---- Row locks without write access ----", 1)[1]
        row_locks = row_locks.split("-- Projection function: API only.", 1)[0]
        scale_locks = row_locks.split("'scale_lineage_guards', 'scale_advice_occurrences'", 1)
        self.assertEqual(len(scale_locks), 2)
        self.assertIn(
            "GRANT UPDATE (id) ON TABLE public.%I TO marketai_domain_api', relation_name",
            scale_locks[1].split("END LOOP;", 1)[0],
        )

    def test_domain_role_replay_lets_the_api_consume_spend_guardrail_previews(self):
        """Confirm consumes a stored preview through two columns (#386); the
        replay revokes everything first, so the file must re-grant them."""
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        block = sql.split("DO $spend_guardrail_preview_grants$", 1)[1]
        block = block.split("END $spend_guardrail_preview_grants$;", 1)[0]
        for statement in (
            "GRANT SELECT ON TABLE public.spend_guardrail_previews "
            "TO marketai_domain_api, marketai_domain_readonly",
            "GRANT INSERT ON TABLE public.spend_guardrail_previews TO marketai_domain_api",
            "GRANT UPDATE (consumed_at, result_revision) ON TABLE public.spend_guardrail_previews "
            "TO marketai_domain_api",
        ):
            self.assertIn(statement, block)
        self.assertNotIn("marketai_domain_worker", block.split("BEGIN", 1)[1])
        self.assertNotIn("DELETE", block.split("BEGIN", 1)[1])

    def test_domain_role_replay_allows_worker_to_validate_workspace(self):
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        select_worker = sql.split("-- ---- SELECT: Worker ----", 1)[1]
        select_worker = select_worker.split("-- ---- INSERT: Worker ----", 1)[0]

        self.assertIn("'workspaces'", select_worker)

    def test_domain_role_replay_allows_worker_to_revalidate_localization_winners(self):
        # Every Localization job re-runs evaluate_winner_identity() as the
        # Worker; without one of these reads the job fails with "permission
        # denied" and the Batch operation ends failed.
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        select_worker = sql.split("-- ---- SELECT: Worker ----", 1)[1]
        select_worker = select_worker.split("-- ---- INSERT: Worker ----", 1)[0]

        for table in (
            "experiment_decisions",
            "judgements",
            "decision_confirmations",
            "experiment_versions",
            "experiment_version_candidates",
            "experiment_eligibility_events",
            "experiment_eligibility_projections",
            "contextual_verdict_projections",
            "asset_versions",
            "library_versions",
        ):
            self.assertIn(f"'{table}'", select_worker)

    def test_domain_role_replay_allows_worker_to_create_manual_fact_drafts(self):
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        insert_worker = sql.split("-- ---- INSERT: Worker ----", 1)[1]
        insert_worker = insert_worker.split("-- ---- UPDATE: Worker ----", 1)[0]

        self.assertIn("'library_entries'", insert_worker)
        self.assertIn("'library_drafts'", insert_worker)

    def test_domain_role_replay_lets_the_api_publish_an_adopted_localized_version(self):
        # Adopting an isolated Localization output publishes its child
        # AssetVersion in the API transaction (#46). Without this INSERT every
        # adoption on PostgreSQL is a 500, and SQLite checks no grants.
        sql = (REPO / "deploy" / "postgres" / "domain-tenant-roles.sql").read_text(
            encoding="utf-8"
        )
        insert_api = sql.split("-- ---- INSERT: API ----", 1)[1]
        insert_api = insert_api.split("-- ---- SELECT: Worker ----", 1)[0]

        self.assertIn("'asset_versions'", insert_api)

    def test_the_cross_database_denials_are_applied_by_bootstrap_only(self):
        denials = REPO / "deploy" / "postgres" / "cross-database-denials.sql"
        bootstrap = (REPO / "deploy" / "postgres" / "apply-roles.sh").read_text(
            encoding="utf-8"
        )
        domain_only = (
            REPO / "deploy" / "postgres" / "apply-domain-roles.sh"
        ).read_text(encoding="utf-8")

        self.assertTrue(denials.exists())
        self.assertIn("REVOKE ALL ON DATABASE marketai_control", denials.read_text(
            encoding="utf-8"
        ))
        self.assertIn("cross-database-denials.sql", bootstrap)
        self.assertNotIn("cross-database-denials.sql", domain_only)

    def test_bootstrap_and_release_stay_separate_entry_points(self):
        # db-roles bootstraps both deployment units; db-release converges one.
        makefile = (REPO / "Makefile").read_text(encoding="utf-8")
        release = (REPO / "deploy" / "postgres" / "release.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("apply-roles.sh", makefile)
        self.assertIn("apply-domain-roles.sh", release)

        executable = [
            line
            for line in release.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertFalse(
            [line for line in executable if "/apply-roles.sh" in line],
            "a domain release must not run the whole-topology bootstrap",
        )

    def test_ci_releases_through_the_same_entry_point(self):
        # A release path only operators run is a release path that breaks in
        # production; a CI path only CI runs proves nothing about it.
        workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("make db-release", workflow)


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
