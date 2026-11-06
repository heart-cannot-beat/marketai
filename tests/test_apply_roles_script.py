"""`apply-roles.sh` must hand the operator's connection string to psql intact.

The script transmits seven role passwords. It used to build the second
connection by string surgery -- ``"${SUPERUSER_DATABASE_URL%/*}/marketai_domain"``
-- which cut the URL at the last slash. On the three connection strings below
that silently dropped ``?sslmode=require``, rewrote the tail of a parameter
value that itself contained a slash, and destroyed a URL with no path at all.
The first of those downgrades TLS while the passwords are in flight, and CI
never saw it because CI uses a URL with no query string.

These tests run the real script against a stub ``psql`` that records its
arguments, so they assert what the script actually invokes rather than
re-deriving the logic.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "postgres" / "apply-roles.sh"
DEPLOY = REPO / "deploy" / "postgres"

PASSWORDS = {
    "CONTROL_OWNER_PASSWORD": "co",
    "CONTROL_APP_PASSWORD": "ca",
    "DOMAIN_OWNER_PASSWORD": "do",
    "DOMAIN_APP_PASSWORD": "da",
    "DOMAIN_API_PASSWORD": "dapi",
    "DOMAIN_WORKER_PASSWORD": "dw",
    "DOMAIN_READONLY_PASSWORD": "dr",
    "MARKETAI_TENANT_CONTEXT_SECRET": "test-tenant-context-secret-32-bytes",
}

STUB_PSQL = """#!/usr/bin/env bash
{
  echo "--- invocation"
  for arg in "$@"; do printf '%s\\n' "$arg"; done
} >> "$PSQL_ARG_LOG"
"""


class ApplyRolesConnectionStringTests(unittest.TestCase):
    def run_script(self, env_overrides: dict[str, str]):
        """Run apply-roles.sh with a stub psql; return the recorded invocations."""
        workdir = tempfile.mkdtemp()
        self.addCleanup(_rmtree, workdir)

        bindir = pathlib.Path(workdir) / "bin"
        bindir.mkdir()
        stub = bindir / "psql"
        stub.write_text(STUB_PSQL, encoding="utf-8")
        stub.chmod(0o755)

        log = pathlib.Path(workdir) / "psql-args.log"

        env = dict(os.environ)
        env.pop("SUPERUSER_DATABASE_URL", None)
        env.update(PASSWORDS)
        env.update(env_overrides)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["PSQL_ARG_LOG"] = str(log)

        result = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
        )
        self.assertEqual(
            result.returncode, 0, f"script failed: {result.stderr}"
        )

        if not log.exists():
            return []

        invocations: list[list[str]] = []
        for line in log.read_text(encoding="utf-8").splitlines():
            if line == "--- invocation":
                invocations.append([])
            else:
                invocations[-1].append(line)
        return invocations

    def assert_url_survives(self, url: str) -> None:
        invocations = self.run_script({"SUPERUSER_DATABASE_URL": url})

        self.assertEqual(
            len(invocations),
            1,
            "both files run in one psql process, so the connection is opened once",
        )

        args = invocations[0]
        self.assertIn("-d", args)
        self.assertEqual(
            args[args.index("-d") + 1],
            url,
            "the operator's connection string must reach psql byte for byte",
        )

        # The failure being guarded is a *derived* second URL. Nothing but the
        # caller's own string may look like a connection string.
        connection_shaped = [a for a in args if "://" in a]
        self.assertEqual(connection_shaped, [url])

        files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
        self.assertEqual(
            [pathlib.Path(f).name for f in files],
            [
                "control-plane-domain-roles.sql",
                "domain-tenant-roles.sql",
                # Names both databases, so it belongs to bootstrap rather than
                # to either unit's release, and it runs last because it revokes
                # from roles the file before it creates.
                "cross-database-denials.sql",
            ],
            "each file depends on what the one before it created",
        )

    def test_a_query_string_is_not_dropped(self):
        # The one that matters most: losing sslmode=require downgrades the
        # connection carrying seven role passwords, and libpq's default
        # (`prefer`) will happily fall back to plaintext.
        self.assert_url_survives(
            "postgresql://postgres:pw@db.example.com:5432/postgres?sslmode=require"
        )

    def test_a_parameter_value_containing_a_slash_is_not_rewritten(self):
        self.assert_url_survives(
            "postgresql://postgres:pw@db.example.com:5432/postgres"
            "?sslmode=verify-full&sslrootcert=/etc/ssl/ca.pem"
        )

    def test_a_url_with_no_path_is_not_destroyed(self):
        self.assert_url_survives("postgresql://postgres@db.example.com:5432")

    def test_without_a_url_psql_is_left_to_the_libpq_environment(self):
        invocations = self.run_script({})

        self.assertEqual(len(invocations), 1)
        self.assertNotIn(
            "-d",
            invocations[0],
            "with no SUPERUSER_DATABASE_URL the PGHOST/PGUSER environment is the"
            " connection; naming a database here would override it",
        )

    def test_a_missing_password_stops_the_script(self):
        env = dict(PASSWORDS)
        del env["DOMAIN_READONLY_PASSWORD"]

        workdir = tempfile.mkdtemp()
        self.addCleanup(_rmtree, workdir)
        full = dict(os.environ)
        full.pop("SUPERUSER_DATABASE_URL", None)
        full.pop("DOMAIN_READONLY_PASSWORD", None)
        full.update(env)
        full["PSQL_ARG_LOG"] = str(pathlib.Path(workdir) / "unused.log")

        result = subprocess.run(
            ["bash", str(SCRIPT)], env=full, capture_output=True, text=True
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DOMAIN_READONLY_PASSWORD", result.stderr)


class ConnectionReuseTests(unittest.TestCase):
    """Every database switch must reuse the connection, not rebuild it."""

    def test_each_database_switch_reuses_the_open_connection(self):
        for name in ("control-plane-domain-roles.sql", "domain-tenant-roles.sql"):
            body = (DEPLOY / name).read_text(encoding="utf-8")
            switches = [
                line.strip()
                for line in body.splitlines()
                if line.strip().startswith("\\connect")
            ]

            with self.subTest(file=name):
                self.assertTrue(switches, "expected at least one \\connect")
                for switch in switches:
                    # -reuse-previous=on is what carries sslmode across the
                    # switch. Stated explicitly rather than left to psql's
                    # default, because the default depends on how the caller
                    # spelled the connection.
                    self.assertIn("-reuse-previous=on", switch)

    def test_the_script_never_takes_the_connection_string_apart(self):
        body = SCRIPT.read_text(encoding="utf-8")
        code = [
            line
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        for line in code:
            self.assertNotIn(
                "SUPERUSER_DATABASE_URL%",
                line,
                "deriving a per-database URL by trimming at the last slash is the"
                " defect this file exists to prevent",
            )


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
