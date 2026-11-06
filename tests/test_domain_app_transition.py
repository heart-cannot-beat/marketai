"""The executable exit gate for the retired shared PostgreSQL runtime role.

FastAPI and the Worker now use distinct roles and every domain unit of work is
TenantContext-bound. ``marketai_domain_app`` is retained only so releases can
converge old clusters from the former bridge state to explicit
``NOBYPASSRLS``. Two things keep that retirement real rather than aspirational:

* ``rolbypassrls`` is persistent cluster state, so the bridge is retired by
  *declaring the opposite* -- editing the statement to ``NOBYPASSRLS`` -- not by
  deleting a line. Deleting it would leave every provisioned cluster exempt
  forever. The gate requires the statement to exist and to say one or the other.
* what counts as an untenanted entry point is decided by parsing ``app/``, not
  by searching it for a string. A constant's own definition and a docstring that
  names it are not call sites; an untenanted ``session_scope()`` is one even
  though it never mentions the Workspace. The gate now requires the count to
  remain zero so a later regression cannot silently revive the shared path.
"""

from __future__ import annotations

import ast
import os
import pathlib
import unittest
import uuid
from dataclasses import dataclass

from scripts.domain_bridge import declared_bridge_state, declared_bridge_statement

REPO = pathlib.Path(__file__).resolve().parents[1]
ROLES_SQL = REPO / "deploy" / "postgres" / "domain-tenant-roles.sql"
APP = REPO / "app"

WORKSPACE_CONSTANT = "DEFAULT_WORKSPACE_ID"
UNTENANTED_UNIT_OF_WORK = "session_scope"

#: Attribute access on these never means the module-level unit of work.
NOT_A_MODULE = frozenset({"self", "cls"})

#: What the scanner reports for the untenanted unit of work.
USES = "uses the untenanted session_scope()"


@dataclass(frozen=True)
class EntryPoint:
    """A place that reaches the database without naming a Workspace."""

    path: str
    line: int
    kind: str

    def __str__(self) -> str:  # pragma: no cover - failure messages only
        return f"{self.path}:{self.line} ({self.kind})"


def _imported_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """What this module calls the constant and the untenanted unit of work.

    ``from ...config import DEFAULT_WORKSPACE_ID as WS`` makes ``WS`` a read of
    the constant, and a scanner that only knows the canonical spelling would
    report the module clean while it is not. Imports are collected with
    ``walk`` rather than from the module body, because an import inside a
    function binds a name just as well.
    """
    constants = {WORKSPACE_CONSTANT}
    units_of_work = {UNTENANTED_UNIT_OF_WORK}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue

        for alias in node.names:
            local = alias.asname or alias.name

            if alias.name == WORKSPACE_CONSTANT:
                constants.add(local)
            elif alias.name == UNTENANTED_UNIT_OF_WORK:
                units_of_work.add(local)

    return constants, units_of_work


def _dotted_name(node: ast.AST) -> str | None:
    """'a.b.c' for a chain of plain names, None for anything else.

    ``database.session_scope`` is the module function. ``Cache().session_scope``
    is an attribute of some object that happens to share the name, and
    ``self.session_scope`` is an attribute of an instance. Only the first is a
    chain of names.
    """
    parts = []

    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value

    if not isinstance(node, ast.Name):
        return None

    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_module_attribute(node: ast.Attribute, attribute: str) -> bool:
    if node.attr != attribute:
        return False

    dotted = _dotted_name(node.value)
    return dotted is not None and dotted.split(".")[0] not in NOT_A_MODULE


def _propagate(tree: ast.AST, constants: set[str], units_of_work: set[str]) -> None:
    """Follow plain assignment aliases to a fixed point.

    ``uow = session_scope`` then ``uow()`` is ordinary static rebinding, not the
    dynamic indirection an AST cannot see. Chains (``a = session_scope; b = a``)
    resolve because the pass repeats until nothing new binds.
    """
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]

    changed = True
    while changed:
        changed = False

        for node in assignments:
            value = node.value

            if isinstance(value, ast.Name):
                source = (
                    units_of_work
                    if value.id in units_of_work
                    else constants
                    if value.id in constants
                    else None
                )
            elif isinstance(value, ast.Attribute):
                source = (
                    units_of_work
                    if _is_module_attribute(value, UNTENANTED_UNIT_OF_WORK)
                    else constants
                    if value.attr == WORKSPACE_CONSTANT
                    else None
                )
            else:
                source = None

            if source is None:
                continue

            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in source:
                    source.add(target.id)
                    changed = True


def _references(tree: ast.AST) -> list[tuple[int, str]]:
    constants, units_of_work = _imported_names(tree)
    _propagate(tree, constants, units_of_work)

    found = []

    for node in ast.walk(tree):
        # Every use is a Load, a call included -- `f()` loads `f` first. Naming
        # the unit of work without calling it counts too: `uow = session_scope`
        # is how an untenanted path gets built out of a name the scanner would
        # otherwise never see again.
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in constants:
                found.append((node.lineno, f"reads {WORKSPACE_CONSTANT}"))
            elif node.id in units_of_work:
                found.append(
                    (node.lineno, f"uses the untenanted {UNTENANTED_UNIT_OF_WORK}()")
                )
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            # Only the canonical spelling for the constant: an alias is a local
            # name, so `x.WS` is some other object's attribute.
            if node.attr == WORKSPACE_CONSTANT:
                found.append((node.lineno, f"reads {WORKSPACE_CONSTANT}"))
            elif _is_module_attribute(node, UNTENANTED_UNIT_OF_WORK):
                found.append(
                    (node.lineno, f"uses the untenanted {UNTENANTED_UNIT_OF_WORK}()")
                )

    return sorted(set(found))


def untenanted_entry_points() -> list[EntryPoint]:
    """Every place in ``app/`` that still reaches data without a TenantContext.

    Nothing is excluded by file. The definition of the constant and the
    definition of ``session_scope`` are not matched because a Store target and a
    ``def`` are not a read and not a call -- so a real use appearing inside
    ``config.py`` or ``database.py`` later is still counted, which a file-level
    exclusion would have hidden.
    """
    entry_points = []

    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = str(path.relative_to(REPO))

        for line, kind in _references(tree):
            entry_points.append(EntryPoint(relative, line, kind))

    return entry_points


class TransitionExitGateTests(unittest.TestCase):
    """Holds without a PostgreSQL cluster, which is the point of a gate."""

    def test_the_bridge_state_is_declared_rather_than_implied(self):
        # Absence is not "no exemption": rolbypassrls persists, so a file that
        # says nothing leaves an upgraded cluster exactly as it was.
        self.assertIn(
            declared_bridge_state(),
            ("BYPASSRLS", "NOBYPASSRLS"),
            "deploy/postgres/domain-tenant-roles.sql must contain exactly one "
            "ALTER ROLE marketai_domain_app (NO)BYPASSRLS statement. Deleting it "
            "does not revoke anything on a cluster that already ran it.",
        )

    def test_the_declared_state_matches_the_untenanted_entry_points(self):
        remaining = untenanted_entry_points()
        declared = declared_bridge_state()

        if remaining:
            self.assertEqual(
                declared,
                "BYPASSRLS",
                f"{len(remaining)} places still reach data without a "
                f"TenantContext (e.g. {[str(e) for e in remaining[:3]]}), so they "
                "connect with no marketai.workspace_id set. Declaring NOBYPASSRLS "
                "now makes every one of their reads empty and every write fail.",
            )
        else:
            self.assertEqual(
                declared,
                "NOBYPASSRLS",
                "nothing in app/ reads DEFAULT_WORKSPACE_ID or opens an "
                "untenanted session_scope() any more, so the bridge has outlived "
                "its reason. Change the statement in "
                "deploy/postgres/domain-tenant-roles.sql to NOBYPASSRLS -- "
                "deleting it would leave provisioned clusters exempt -- and point "
                "FastAPI and the Worker at marketai_domain_api and "
                "marketai_domain_worker.",
            )

    def test_the_bridge_is_labelled_and_names_its_own_gate(self):
        body = ROLES_SQL.read_text(encoding="utf-8")

        if declared_bridge_state() == "BYPASSRLS":
            self.assertIn("TRANSITIONAL", body)
        else:
            self.assertIn("retired compatibility role", body)
        self.assertIn(
            "tests/test_domain_app_transition.py",
            body,
            "an exemption this broad must point at the check that retires it",
        )


class EntryPointDetectionTests(unittest.TestCase):
    """The gate is only as good as what it counts. Assert what it counts.

    Both error directions matter, but not equally. Missing an entry point
    retires the bridge while an untenanted path is still live, which is the
    outage; counting one that is not real only blocks retirement until the
    scanner is corrected. Where a judgement call remains, it leans towards
    counting.
    """

    def parse(self, source: str) -> list[str]:
        return [kind for _, kind in _references(ast.parse(source))]

    # ------------------------------------------------ what is not a use ----

    def test_a_constant_definition_is_not_an_entry_point(self):
        # app/config.py:17 -- the declaration itself must never keep the bridge
        # alive, or the exemption could never be retired.
        self.assertEqual(
            self.parse('DEFAULT_WORKSPACE_ID = os.getenv("X", "ws_default")'), []
        )

    def test_defining_the_unit_of_work_is_not_an_entry_point(self):
        # app/adapters/persistence/database.py -- a def is not a call.
        self.assertEqual(
            self.parse("def session_scope():\n    yield 1\n"), []
        )

    def test_prose_naming_them_is_not_an_entry_point(self):
        # app/platform/tenancy.py's module docstring explains why the constant
        # is not a tenant boundary. Explaining it is not using it.
        self.assertEqual(
            self.parse('"""DEFAULT_WORKSPACE_ID and session_scope() explained."""'),
            [],
        )
        self.assertEqual(self.parse('x = "DEFAULT_WORKSPACE_ID"'), [])

    def test_a_re_export_is_not_an_entry_point(self):
        # app/adapters/persistence/__init__.py imports it for others to use.
        self.assertEqual(self.parse("from .database import session_scope"), [])

    def test_a_same_named_method_on_another_object_is_not_an_entry_point(self):
        # The name is not reserved; only the module-level function is the
        # untenanted unit of work.
        self.assertEqual(self.parse("Cache().session_scope()"), [])
        self.assertEqual(self.parse("self.session_scope()"), [])
        self.assertEqual(self.parse("cls.session_scope()"), [])

    def test_an_alias_of_the_constant_is_not_read_off_other_objects(self):
        # `WS` is this module's local name for the constant. `x.WS` is some
        # other object's attribute that happens to be spelled the same.
        self.assertEqual(
            self.parse("from .config import DEFAULT_WORKSPACE_ID as WS\nx.WS\n"),
            [],
        )

    # ---------------------------------------------------- what is a use ----

    def test_a_read_through_the_config_module_is_an_entry_point(self):
        self.assertEqual(
            self.parse("ws = config.DEFAULT_WORKSPACE_ID"),
            ["reads DEFAULT_WORKSPACE_ID"],
        )

    def test_a_bare_read_after_a_from_import_is_an_entry_point(self):
        self.assertEqual(
            self.parse(
                "from .config import DEFAULT_WORKSPACE_ID\nws = DEFAULT_WORKSPACE_ID\n"
            ),
            ["reads DEFAULT_WORKSPACE_ID"],
        )

    def test_a_renamed_constant_is_an_entry_point(self):
        # Renaming on import is invisible to a scanner that only knows the
        # canonical spelling, and the module would read clean while it is not.
        self.assertEqual(
            self.parse("from .config import DEFAULT_WORKSPACE_ID as WS\nws = WS\n"),
            ["reads DEFAULT_WORKSPACE_ID"],
        )

    def test_an_untenanted_unit_of_work_is_an_entry_point(self):
        # Fixed-tenant access that never mentions the Workspace: a search for
        # the constant would miss it entirely.
        self.assertEqual(
            self.parse("with session_scope() as session:\n    pass\n"),
            [USES],
        )

    def test_a_renamed_unit_of_work_is_an_entry_point(self):
        self.assertEqual(
            self.parse(
                "from .database import session_scope as uow\n"
                "with uow() as session:\n    pass\n"
            ),
            [USES],
        )

    def test_a_unit_of_work_called_through_its_module_is_an_entry_point(self):
        for source in (
            "from . import database\nwith database.session_scope() as s:\n    pass\n",
            "from . import database as db\nwith db.session_scope() as s:\n    pass\n",
        ):
            with self.subTest(source=source.splitlines()[0]):
                self.assertEqual(self.parse(source), [USES])

    def test_an_assignment_alias_is_followed(self):
        # Plain static rebinding, not the dynamic indirection an AST cannot
        # see: `uow = session_scope` then `uow()`. Missing it retires the bridge
        # with an untenanted path still live.
        self.assertEqual(
            self.parse(
                "from .database import session_scope\nuow = session_scope\nuow()\n"
            ),
            [USES, USES],
        )

    def test_an_assignment_alias_through_a_module_is_followed(self):
        self.assertEqual(
            self.parse(
                "from . import database\nuow = database.session_scope\nuow()\n"
            ),
            [USES, USES],
        )

    def test_a_chain_of_assignment_aliases_is_followed(self):
        # The binding pass repeats until nothing new binds, so depth is not a
        # place to hide.
        self.assertEqual(
            self.parse(
                "from .database import session_scope\na = session_scope\n"
                "b = a\nb()\n"
            ),
            [USES, USES, USES],
        )

    def test_naming_the_unit_of_work_without_calling_it_is_an_entry_point(self):
        # A name that escapes the module is a path the scanner cannot follow
        # any further, so the reference itself is the entry point.
        self.assertEqual(
            self.parse("from .database import session_scope\nregister(session_scope)\n"),
            [USES],
        )

    def test_an_alias_of_something_else_is_not_followed(self):
        self.assertEqual(self.parse("uow = Cache().session_scope\nuow()\n"), [])

    def test_an_import_inside_a_function_still_binds_the_name(self):
        self.assertEqual(
            self.parse(
                "def f():\n"
                "    from .config import DEFAULT_WORKSPACE_ID as WS\n"
                "    return WS\n"
            ),
            ["reads DEFAULT_WORKSPACE_ID"],
        )

    # ------------------------------------------------------------ canary ----

    def test_nothing_is_excluded_by_file(self):
        # A whole-file exclusion hides a real use that appears in that file
        # later. The definitions are skipped because a Store and a def are
        # neither a read nor a call -- not because of where they live.
        definition_files = ("app/config.py", "app/adapters/persistence/database.py")

        for name in definition_files:
            source = (REPO / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertEqual(_references(ast.parse(source)), [])

        # ... and a real use inside one of them would be counted.
        self.assertEqual(
            self.parse("def helper():\n    return DEFAULT_WORKSPACE_ID\n"),
            ["reads DEFAULT_WORKSPACE_ID"],
        )

    def test_the_runtime_has_no_untenanted_call_sites_left(self):
        self.assertEqual(untenanted_entry_points(), [])


OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")
APP_URL = os.environ.get("DOMAIN_APP_DATABASE_URL")
SUPERUSER_URL = os.environ.get("DOMAIN_SUPERUSER_DATABASE_URL")

CONFIGURED = all((OWNER_URL, APP_URL))

SKIP_REASON = (
    "set DOMAIN_OWNER_DATABASE_URL and DOMAIN_APP_DATABASE_URL against a "
    "cluster provisioned with deploy/postgres/apply-roles.sh and migrated to head"
)

INSERT_WORKSPACE = """
INSERT INTO workspaces (
    id, name, default_currency, lifecycle, budget_currency, created_at
)
VALUES (:id, :id, 'USD', 'active', 'USD', now())
ON CONFLICT (id) DO NOTHING
"""

INSERT_PRODUCT = """
INSERT INTO products (id, workspace_id, name, description, selling_points,
                      audience, brand_notes, ref_images, economics, revision,
                      created_at, updated_at)
VALUES (:id, :workspace_id, :name, '', '[]', '', '', '[]', '{}', 1, now(), now())
"""


class _ClusterFixture:
    """One Workspace and one row, reachable as the owner. Not a test case."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()

        from sqlalchemy import create_engine, text

        cls.create_engine = staticmethod(create_engine)
        cls.text = staticmethod(text)

        cls.workspace = f"ws_legacy_{uuid.uuid4().hex[:8]}"
        cls.product = f"p_{uuid.uuid4().hex[:8]}"
        cls.owner = create_engine(OWNER_URL, future=True)

        with cls.owner.begin() as conn:
            conn.execute(text(INSERT_WORKSPACE), {"id": cls.workspace})
            conn.execute(
                text(INSERT_PRODUCT),
                {
                    "id": cls.product,
                    "workspace_id": cls.workspace,
                    "name": "Legacy",
                },
            )

    @classmethod
    def tearDownClass(cls) -> None:
        from sqlalchemy import text

        with cls.owner.begin() as conn:
            conn.execute(
                text("DELETE FROM products WHERE workspace_id = :w"),
                {"w": cls.workspace},
            )
            conn.execute(
                text("DELETE FROM workspaces WHERE id = :w"), {"w": cls.workspace}
            )

        cls.owner.dispose()

    def app_connection(self):
        """Connect as the retired shared role, with no tenant setting."""
        engine = self.create_engine(APP_URL, future=True)
        self.addCleanup(engine.dispose)
        conn = engine.connect()
        self.addCleanup(conn.close)
        return conn

    def app_can_read(self) -> bool:
        try:
            conn = self.app_connection()
            rows = conn.execute(
                self.text("SELECT id FROM products WHERE workspace_id = :w"),
                {"w": self.workspace},
            ).scalars().all()
        except Exception:
            # The retired role is intentionally not granted EXECUTE on the new
            # epoch-aware policy helper. Permission denied is an even stronger
            # fail-closed result than an empty policy-filtered result.
            return False
        return rows == [self.product]

    def app_can_write(self) -> bool:
        try:
            conn = self.app_connection()
            transaction = conn.begin()
            conn.execute(
                self.text(INSERT_PRODUCT),
                {
                    "id": f"p_{uuid.uuid4().hex[:8]}",
                    "workspace_id": self.workspace,
                    "name": "Written",
                },
            )
        except Exception:
            return False
        finally:
            if "transaction" in locals() and transaction.is_active:
                transaction.rollback()
        return True

    def cluster_bypasses(self) -> bool:
        with self.owner.begin() as conn:
            return bool(
                conn.execute(
                    self.text(
                        "SELECT rolbypassrls FROM pg_roles"
                        " WHERE rolname = 'marketai_domain_app'"
                    )
                ).scalar()
            )


@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class RetiredRuntimePathFailsClosedTests(_ClusterFixture, unittest.TestCase):
    """The old shared application role is unusable after the cut-over."""

    def test_the_retired_role_reads_nothing(self):
        self.assertFalse(self.app_can_read())

    def test_the_retired_role_cannot_write(self):
        self.assertFalse(self.app_can_write())

    def test_the_retired_role_has_no_domain_acl(self):
        with self.owner.connect() as conn:
            schema_usage, product_select = conn.execute(
                self.text(
                    "SELECT has_schema_privilege('marketai_domain_app', 'public', 'USAGE'), "
                    "has_table_privilege('marketai_domain_app', 'public.products', 'SELECT')"
                )
            ).one()
        self.assertFalse(schema_usage)
        self.assertFalse(product_select)

        body = ROLES_SQL.read_text(encoding="utf-8")
        self.assertIn(
            "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM marketai_domain_app",
            body,
        )

    def test_the_cluster_agrees_with_the_declared_state(self):
        # Drift detection. A cluster that disagrees with the file is a cluster
        # nobody can reason about from the repository.
        self.assertEqual(
            self.cluster_bypasses(),
            declared_bridge_state() == "BYPASSRLS",
            "marketai_domain_app's rolbypassrls does not match the state declared "
            "in deploy/postgres/domain-tenant-roles.sql; re-run make db-roles",
        )

    def test_the_bridge_is_the_only_extra_exemption(self):
        # Whatever else the transition costs, it must not have widened to the
        # roles that are supposed to be isolated.
        with self.owner.begin() as conn:
            bypassing = sorted(
                r[0]
                for r in conn.execute(
                    self.text(
                        "SELECT rolname FROM pg_roles WHERE rolbypassrls"
                        " AND rolname LIKE 'marketai_%'"
                    )
                )
            )

        expected = ["marketai_domain_owner"]
        if declared_bridge_state() == "BYPASSRLS":
            expected = sorted(expected + ["marketai_domain_app"])

        self.assertEqual(bypassing, expected)


@unittest.skipUnless(
    CONFIGURED and SUPERUSER_URL,
    "also set DOMAIN_SUPERUSER_DATABASE_URL: only a superuser may change BYPASSRLS",
)
class BridgeRevocationDrillTests(_ClusterFixture, unittest.TestCase):
    """Rehearse the retirement on the cluster that already has the exemption.

    Provisioning a fresh cluster proves the grant is applied. It says nothing
    about the case that actually matters at retirement time: a cluster running
    in production with ``rolbypassrls`` already true, being upgraded. Role
    attributes are persistent, so that transition has to be an explicit
    statement and it has to be exercised.
    """

    def superuser(self):
        engine = self.create_engine(SUPERUSER_URL, future=True, poolclass=None)
        self.addCleanup(engine.dispose)
        return engine

    def set_bridge(self, state: str) -> None:
        engine = self.superuser()
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT").execute(
                self.text(f"ALTER ROLE marketai_domain_app {state}")
            )

    def test_the_declared_statement_moves_a_cluster_that_is_already_provisioned(self):
        declared = declared_bridge_state()
        opposite = "NOBYPASSRLS" if declared == "BYPASSRLS" else "BYPASSRLS"

        self.addCleanup(self.set_bridge, declared)

        # Put the cluster in the state it would be in on the far side of the
        # transition, and confirm the change is real rather than nominal.
        self.set_bridge(opposite)
        self.assertEqual(self.cluster_bypasses(), opposite == "BYPASSRLS")

        # Replay provisioning's own statement, character for character.
        engine = self.superuser()
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT").execute(
                self.text(declared_bridge_statement())
            )

        self.assertEqual(self.cluster_bypasses(), declared == "BYPASSRLS")
        self.assertFalse(
            self.app_can_read(),
            "the retired role must stay unusable even while the drill toggles its attribute",
        )

    def test_retiring_the_bridge_is_what_closes_the_untenanted_path(self):
        # The claim the gate rests on, stated as a test: with the exemption
        # gone, the path that never sets marketai.workspace_id reads nothing.
        self.addCleanup(self.set_bridge, declared_bridge_state())
        self.set_bridge("NOBYPASSRLS")

        self.assertFalse(self.app_can_read())

        self.assertFalse(self.app_can_write())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
