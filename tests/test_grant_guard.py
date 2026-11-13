"""The grant guard's own parts (#59): the role-file evaluator, the statement
classifier and the check. ``tests/test_grant_matrix_pg.py`` proves the
evaluator against a released PostgreSQL; ``tests/conftest.py`` runs the check
over the whole suite."""
from __future__ import annotations

import ast
import asyncio
import unittest
from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table, create_engine, func, insert, select, update

from app.platform import runtime_role
from app.platform.runtime_role import API_ROLE, WORKER_ROLE, current_role, entered_as, runs_as
from tests.support.grant_guard import (
    KNOWN_GAPS,
    SQLITE_ONLY_WRITES,
    GrantGuard,
    KnownGap,
    Read,
    SqliteOnlyWrite,
    classify,
    qualified_columns,
    reads,
)
from tests.support.grant_matrix import (
    ROLE_FILE,
    GrantScriptError,
    evaluate,
    load_grant_matrix,
)


class RoleFileEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = load_grant_matrix()

    def test_loops_and_conditions_are_evaluated(self):
        # FOREACH over the INSERT: API list, including #46's asset_versions.
        self.assertTrue(self.matrix.allows(API_ROLE, "asset_versions", "INSERT"))
        self.assertTrue(self.matrix.allows(WORKER_ROLE, "asset_versions", "INSERT"))
        # The Scale SLICE matrix: API inserts policy versions, the Worker reads them.
        self.assertTrue(self.matrix.allows(API_ROLE, "scale_policy_versions", "INSERT"))
        self.assertFalse(self.matrix.allows(WORKER_ROLE, "scale_policy_versions", "INSERT"))
        self.assertTrue(self.matrix.allows(WORKER_ROLE, "scale_policy_versions", "SELECT"))
        # Nobody deletes.
        for role in (API_ROLE, WORKER_ROLE):
            for table in self.matrix.roles[role]:
                self.assertFalse(self.matrix.allows(role, table, "DELETE"), (role, table))

    def test_column_grants_allow_only_their_columns(self):
        edits = "marketing_image_edits"
        self.assertTrue(self.matrix.allows(WORKER_ROLE, edits, "UPDATE", frozenset({"state", "error_code"})))
        self.assertFalse(self.matrix.allows(WORKER_ROLE, edits, "UPDATE", frozenset({"state", "prompt"})))
        self.assertFalse(self.matrix.allows(WORKER_ROLE, edits, "UPDATE", None))
        # A row-lock-only grant: UPDATE (id) is no write access.
        self.assertTrue(self.matrix.allows(API_ROLE, "scale_lineage_guards", "UPDATE", frozenset({"id"})))
        self.assertFalse(
            self.matrix.allows(API_ROLE, "scale_lineage_guards", "UPDATE", frozenset({"revision"}))
        )

    def test_the_owner_only_context_key_table_is_granted_to_nobody(self):
        for role in (API_ROLE, WORKER_ROLE):
            self.assertFalse(self.matrix.allows(role, "tenant_context_secrets", "SELECT"))

    def test_removing_a_grant_from_the_file_removes_it_from_the_matrix(self):
        # #46's starting point: asset_versions missing from "INSERT: API".
        sql = ROLE_FILE.read_text(encoding="utf-8")
        marker = "    'asset_versions', 'jobs',\n    'cost_ledger', 'meta_accounts',"
        self.assertEqual(sql.count(marker), 1)
        before_46 = evaluate(sql.replace(marker, "    'jobs',\n    'cost_ledger', 'meta_accounts',"))
        self.assertFalse(before_46.allows(API_ROLE, "asset_versions", "INSERT"))
        self.assertTrue(before_46.allows(API_ROLE, "asset_versions", "UPDATE"))

    def test_statements_it_cannot_place_are_refused(self):
        for sql in (
            "GRANT marketai_projection_writer TO marketai_domain_api;",
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO marketai_domain_api;",
            "DO $x$ BEGIN PERFORM 1; END $x$;",
            "DO $x$ BEGIN IF true THEN NULL; ELSIF false THEN NULL; END IF; END $x$;",
            "SELECT 'GRANT INSERT ON jobs TO marketai_domain_api' \\gexec",
            "GRANT INSERT ON other_schema.jobs TO marketai_domain_api;",
        ):
            with self.subTest(sql=sql), self.assertRaises(GrantScriptError):
                evaluate(sql.replace("\\gexec", "\n\\gexec"))

    def test_revoke_all_clears_column_grants_too(self):
        matrix = evaluate(
            "GRANT UPDATE (state) ON jobs TO marketai_domain_worker;\n"
            "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM marketai_domain_worker;\n"
            "DO $g$ DECLARE t text; BEGIN FOREACH t IN ARRAY ARRAY['jobs', 'runs'] LOOP\n"
            "  IF to_regclass(format('public.%I', t)) IS NOT NULL THEN\n"
            "    EXECUTE format('GRANT INSERT ON TABLE public.%I TO marketai_domain_worker', t);\n"
            "  END IF; END LOOP; END $g$;\n"
        )
        self.assertFalse(matrix.allows(WORKER_ROLE, "jobs", "UPDATE", frozenset({"state"})))
        self.assertTrue(matrix.allows(WORKER_ROLE, "runs", "INSERT"))


class StatementClassificationTests(unittest.TestCase):
    def test_orm_and_core_writes(self):
        self.assertEqual(
            classify("INSERT INTO usage_events (id, metric) VALUES (?, ?) ON CONFLICT DO NOTHING"),
            (("INSERT", "usage_events", frozenset({"id", "metric"})),),
        )
        self.assertEqual(
            classify("UPDATE jobs SET state=?, revision=(jobs.revision + ?) WHERE jobs.id = ?"),
            (("UPDATE", "jobs", frozenset({"state", "revision"})),),
        )
        self.assertEqual(classify("DELETE FROM jobs WHERE jobs.id = ?"), (("DELETE", "jobs", None),))

    def test_raw_sql_writes(self):
        self.assertEqual(
            classify(
                "\n  UPDATE upload_intent_records\n     SET state = 'verified', verified_at = :now"
                "\n   WHERE id = :id AND state = 'pending'"
            ),
            (("UPDATE", "upload_intent_records", frozenset({"state", "verified_at"})),),
        )
        self.assertEqual(
            classify("INSERT INTO t (a, b) VALUES (1, 2) ON CONFLICT (a) DO UPDATE SET b = excluded.b"),
            (
                ("INSERT", "t", frozenset({"a", "b"})),
                ("UPDATE", "t", frozenset({"b"})),
                # PostgreSQL reads the conflict target.
                ("SELECT", "t", frozenset({"a"})),
            ),
        )
        self.assertEqual(
            classify("UPDATE jobs SET state=? WHERE jobs.id = ? RETURNING jobs.id, jobs.revision"),
            (("UPDATE", "jobs", frozenset({"state"})), ("SELECT", "jobs", frozenset({"id", "revision"}))),
        )
        self.assertEqual(
            classify("WITH x AS (SELECT 1) DELETE FROM public.t WHERE id IN (SELECT * FROM x)"),
            (("DELETE", "t", None),),
        )

    def test_reads_are_not_writes(self):
        for sql in (
            "SELECT * FROM jobs WHERE id = ? FOR UPDATE",
            "PRAGMA foreign_keys=ON",
            "WITH x AS (SELECT 1) SELECT * FROM x",
        ):
            self.assertEqual(classify(sql), (), sql)


class ReadClassificationTests(unittest.TestCase):
    def test_scanned_tables_with_their_aliases_and_level(self):
        self.assertEqual(
            reads(
                "SELECT a.id, b.x \nFROM jobs AS a JOIN runs AS b ON a.id = b.job_id, other "
                "\nWHERE a.id IN (SELECT c.id FROM checks AS c) FOR UPDATE"
            ),
            (
                Read("jobs", frozenset({"jobs", "a"}), True, True),
                Read("other", frozenset({"other"}), True, True),
                Read("runs", frozenset({"runs", "b"}), True, True),
                Read("checks", frozenset({"checks", "c"}), True, False),
            ),
        )

    def test_ctes_subqueries_functions_and_sqlite_internals_are_not_tables(self):
        self.assertEqual(
            reads(
                "WITH recent AS (SELECT jobs.id FROM jobs) SELECT anon_1.n FROM "
                "(SELECT count(*) AS n FROM recent) AS anon_1, json_each(?) AS j"
            ),
            (Read("jobs", frozenset({"jobs"}), True, False),),
        )
        self.assertEqual(reads("SELECT name FROM sqlite_master WHERE name = 'x FROM y'"), ())
        self.assertEqual(
            reads("SELECT t.a FROM t WHERE t.a IS NOT DISTINCT FROM t.b"),
            (Read("t", frozenset({"t"}), True, True),),
        )

    def test_a_write_target_is_read_by_its_where_and_set_expressions(self):
        self.assertEqual(
            reads("UPDATE jobs SET state=?, revision=(jobs.revision + ?) WHERE jobs.id = ?"),
            (Read("jobs", frozenset({"jobs"}), False),),
        )
        self.assertEqual(
            reads("DELETE FROM jobs WHERE jobs.id IN (SELECT runs.job_id FROM runs)"),
            (Read("runs", frozenset({"runs"}), True, False), Read("jobs", frozenset({"jobs"}), False)),
        )
        self.assertEqual(reads("INSERT INTO jobs (id) VALUES (?)"), ())

    def test_qualified_columns(self):
        sql = (
            "SELECT credential_versions_1.id, credential_versions.state FROM credential_versions "
            "JOIN credential_versions AS credential_versions_1 ON credential_versions_1.id = ? "
            "WHERE credential_versions.workspace_id = 'credential_versions.secret'"
        )
        self.assertEqual(
            qualified_columns(sql, frozenset({"credential_versions"})),
            frozenset({"state", "workspace_id"}),
        )
        self.assertEqual(qualified_columns(sql, frozenset({"credential_versions_1"})), frozenset({"id"}))
        self.assertIsNone(qualified_columns("SELECT t.* FROM t", frozenset({"t"})))


class RoleMarkingTests(unittest.TestCase):
    def test_the_innermost_entry_wins(self):
        self.assertTrue(runtime_role.RECORDING, "tests/conftest.py turns marking on")

        @runs_as(WORKER_ROLE)
        def handler():
            return current_role()

        self.assertIsNone(current_role())
        self.assertEqual(handler(), WORKER_ROLE)
        with entered_as(API_ROLE):
            self.assertEqual(handler(), WORKER_ROLE)
            self.assertEqual(current_role(), API_ROLE)
        self.assertIsNone(current_role())

    def test_async_entries_reach_threads_they_start(self):
        @runs_as(WORKER_ROLE)
        async def handler():
            return await asyncio.to_thread(current_role)

        self.assertEqual(asyncio.run(handler()), WORKER_ROLE)

    def test_no_api_module_calls_a_worker_entry(self):
        """The innermost entry wins, so a Worker entry the API called would
        be checked against the Worker's grants while the API role runs it.
        Only Worker-side modules may name one."""
        app_root = Path(__file__).resolve().parents[1] / "app"
        trees = {
            ".".join(path.relative_to(app_root.parent).with_suffix("").parts): ast.parse(
                path.read_text(encoding="utf-8")
            )
            for path in app_root.rglob("*.py")
        }
        entries: dict[str, set[str]] = {}
        for module, tree in trees.items():
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    isinstance(d, ast.Call) and getattr(d.func, "id", None) == "runs_as"
                    for d in node.decorator_list
                ):
                    entries.setdefault(node.name, set()).add(module)
        self.assertIn("run_scale_evaluation", entries)
        worker_side = {m for modules in entries.values() for m in modules}
        callers = sorted(
            f"{module}:{node.lineno} names {name}"
            for module, tree in trees.items()
            if module not in worker_side
            for node in ast.walk(tree)
            for name in [
                node.id if isinstance(node, ast.Name)
                else node.attr if isinstance(node, ast.Attribute)
                else None
            ]
            # Private names are the runtimes' own handler methods.
            if name in entries and not name.startswith("_")
        )
        self.assertEqual(callers, [])

    def test_no_row_lock_is_taken_only_on_postgresql(self):
        """SQLAlchemy leaves FOR UPDATE out of SQLite's SQL on its own, and
        the guard reads the lock from the statement. A lock added only when
        the dialect is PostgreSQL never reaches the guard."""
        app_root = Path(__file__).resolve().parents[1] / "app"
        hidden = []
        for path in app_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.If, ast.IfExp)):
                    continue
                condition = ast.unparse(node.test)
                if "dialect" not in condition and "postgresql" not in condition:
                    continue
                branch = (
                    ast.Module(body=node.body, type_ignores=[]) if isinstance(node, ast.If) else node.body
                )
                if any(
                    isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "with_for_update"
                    for call in ast.walk(branch)
                ):
                    hidden.append(f"{path.relative_to(app_root.parent)}:{node.lineno}")
        self.assertEqual(hidden, [])

    def test_marking_off_returns_the_function_itself(self):
        def handler():
            return None

        recording = runtime_role.RECORDING
        runtime_role.RECORDING = False
        try:
            self.assertIs(runs_as(WORKER_ROLE)(handler), handler)
        finally:
            runtime_role.RECORDING = recording


def _sending_function(engine, statement):
    with engine.begin() as connection:
        connection.execute(statement)


def _sending_with_secret(engine, statement, *, include_secret=False):
    with engine.begin() as connection:
        connection.execute(statement)


#: Made-up roles, so the suite's own guard (which checks only the API and
#: Worker roles) ignores these writes.
ROLE_A = "grant_guard_test_role_a"
ROLE_B = "grant_guard_test_role_b"


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        metadata = MetaData()
        self.jobs = Table("jobs", metadata, Column("id", String, primary_key=True), Column("state", String))
        self.edits = Table(
            "marketing_image_edits", metadata,
            Column("id", String, primary_key=True), Column("state", String), Column("prompt", String),
        )
        metadata.create_all(self.engine)
        self.matrix = evaluate(
            f"GRANT INSERT, UPDATE ON jobs TO {ROLE_B};\n"
            f"GRANT UPDATE (state) ON marketing_image_edits TO {ROLE_B};\n"
            f"GRANT SELECT (id, state) ON marketing_image_edits TO {ROLE_B};\n"
            f"GRANT SELECT ON jobs TO {ROLE_A};\n"
        )

    def _guard(self, **kwargs) -> GrantGuard:
        guard = GrantGuard(
            self.matrix, lambda: "tests/x.py::test_y", roles=frozenset({ROLE_A, ROLE_B}), **kwargs
        )
        self.addCleanup(guard.install())
        return guard

    def test_a_write_without_a_grant_names_role_table_operation_and_test(self):
        guard = self._guard()
        with entered_as(ROLE_B):
            _sending_function(self.engine, insert(self.jobs).values(id="j1", state="queued"))
            _sending_function(self.engine, update(self.edits).values(state="done"))
            _sending_function(self.engine, update(self.edits).values(prompt="x"))
        with entered_as(ROLE_A):
            _sending_function(self.engine, insert(self.jobs).values(id="j2", state="queued"))
        violations = guard.unreported()
        self.assertEqual(
            sorted((v.role, v.operation, v.table, v.columns) for v in violations),
            [
                (ROLE_A, "INSERT", "jobs", frozenset({"id", "state"})),
                (ROLE_B, "UPDATE", "marketing_image_edits", frozenset({"prompt"})),
            ],
        )
        self.assertTrue(all(v.test == "tests/x.py::test_y" for v in violations))
        self.assertEqual(guard.unreported(), [], "each violation is reported once")

    def test_a_read_needs_select_on_every_column_it_names(self):
        guard = self._guard()
        with entered_as(ROLE_B):
            _sending_function(self.engine, select(self.edits.c.id, self.edits.c.state))
            _sending_function(self.engine, select(func.count()).select_from(self.edits))
            _sending_function(self.engine, select(self.edits.c.id).where(self.edits.c.prompt == "p"))
            _sending_function(self.engine, select(self.jobs.c.id))
        with entered_as(ROLE_A):
            _sending_function(self.engine, select(self.jobs))
        violations = guard.unreported()
        self.assertEqual(
            sorted((v.role, v.operation, v.table, v.columns, v.why) for v in violations),
            [
                (ROLE_B, "SELECT", "jobs", None, "read"),
                (ROLE_B, "SELECT", "marketing_image_edits", frozenset({"prompt"}), "read"),
            ],
        )
        jobs = next(v for v in violations if v.table == "jobs")
        self.assertIn("has no SELECT grant on jobs, which the statement reads", jobs.describe())
        roles = guard.summary()["roles"]
        self.assertEqual(roles[ROLE_B]["reads"], 4)
        self.assertEqual(roles[ROLE_A]["read_tables"], 1)

    def test_a_write_needs_select_on_what_its_where_reads(self):
        guard = self._guard()
        with entered_as(ROLE_B):
            _sending_function(
                self.engine, update(self.edits).where(self.edits.c.id == "e").values(state="done")
            )
            _sending_function(
                self.engine, update(self.edits).where(self.edits.c.prompt == "p").values(state="done")
            )
        self.assertEqual(
            [(v.operation, v.table, v.columns, v.why) for v in guard.unreported()],
            [("SELECT", "marketing_image_edits", frozenset({"prompt"}), "where")],
        )

    def test_a_row_lock_needs_update_on_some_column(self):
        """SQLite leaves FOR UPDATE out of the SQL; the guard reads it from
        the statement."""
        guard = self._guard()
        with entered_as(ROLE_B):
            _sending_function(self.engine, select(self.edits.c.id).with_for_update())
        with entered_as(ROLE_A):
            _sending_function(self.engine, select(self.jobs.c.id))
            _sending_function(self.engine, select(self.jobs.c.id).with_for_update(read=True))
        violations = guard.unreported()
        self.assertEqual(
            [(v.role, v.operation, v.table) for v in violations], [(ROLE_A, "LOCK", "jobs")]
        )
        self.assertIn("no UPDATE grant on any column of jobs", violations[0].describe())
        self.assertEqual(guard.summary()["roles"][ROLE_A]["locks"], 1)

    def test_writes_outside_a_role_are_counted_not_checked(self):
        guard = self._guard()
        _sending_function(self.engine, insert(self.edits).values(id="e1", prompt="p"))
        self.assertEqual(guard.unreported(), [])
        self.assertEqual(guard.summary()["roles"]["unmarked"]["writes"], 1)

    def test_every_exempted_site_still_exists(self):
        import importlib
        import inspect

        conditions = {e.site: e.when for e in SQLITE_ONLY_WRITES if e.when}
        for site in {e.site for e in SQLITE_ONLY_WRITES} | {g.site for g in KNOWN_GAPS}:
            module_name, qualname = site.split(":")
            target = importlib.import_module(module_name)
            for part in qualname.split("."):
                target = getattr(target, part)
            self.assertTrue(callable(target), site)
            if site in conditions:
                self.assertIn(conditions[site][0], inspect.signature(target).parameters, site)

    def test_every_known_gap_is_still_missing_from_the_role_file(self):
        """A grant that lands for a known gap must take its exemption with
        it, or the next write it lets through goes unchecked."""
        matrix = load_grant_matrix()
        for gap in KNOWN_GAPS:
            with self.subTest(gap=gap):
                privileges = matrix.privileges(gap.role, gap.table)
                granted = (
                    privileges.allows_any("UPDATE")
                    if gap.operation == "LOCK"
                    else privileges.allows(gap.operation, None)
                )
                self.assertFalse(granted, f"#{gap.issue} is granted now: remove its KNOWN_GAPS entry")

    def test_exemptions_are_narrow(self):
        site = f"{__name__}:_sending_function"
        guard = self._guard(
            sqlite_only=(SqliteOnlyWrite("marketing_image_edits", "INSERT", site, "test"),),
            known_gaps=(KnownGap(ROLE_A, "jobs", "INSERT", site, 1),),
        )
        with entered_as(ROLE_B):
            _sending_function(self.engine, insert(self.edits).values(id="e2"))
            with self.engine.begin() as connection:  # not the exempted function
                connection.execute(insert(self.edits).values(id="e3"))
        with entered_as(ROLE_A):
            _sending_function(self.engine, insert(self.jobs).values(id="j3"))
        violations = guard.unreported()
        self.assertEqual(
            [(v.role, v.operation, v.table) for v in violations],
            [(ROLE_B, "INSERT", "marketing_image_edits")],
        )
        self.assertIn("test_exemptions_are_narrow", violations[0].site)

    def test_a_conditional_exemption_holds_only_for_its_argument(self):
        guard = self._guard(sqlite_only=(SqliteOnlyWrite(
            "jobs", "SELECT", f"{__name__}:_sending_with_secret", "test", when=("include_secret", False),
        ),))
        with entered_as(ROLE_B):
            _sending_with_secret(self.engine, select(self.jobs.c.id))
            self.assertEqual(guard.unreported(), [])
            _sending_with_secret(self.engine, select(self.jobs.c.id), include_secret=True)
        self.assertEqual(
            [(v.role, v.operation, v.table) for v in guard.unreported()], [(ROLE_B, "SELECT", "jobs")]
        )


if __name__ == "__main__":
    unittest.main()
