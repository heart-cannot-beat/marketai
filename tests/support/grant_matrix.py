"""The table privileges ``deploy/postgres/domain-tenant-roles.sql`` leaves (#59).

The role file revokes every runtime role's table privileges and grants the
declared matrix back, partly through PL/pgSQL loops over table lists and
conditional ``EXECUTE format(...)`` statements. :func:`load_grant_matrix`
evaluates the subset of psql and PL/pgSQL the file uses, statement by
statement, so the SQLite suite can check what API and Worker code writes
against what PostgreSQL will allow, without a PostgreSQL server.

The evaluator is strict: a ``GRANT`` or ``REVOKE`` it cannot place, or a
PL/pgSQL construct it does not know, raises :class:`GrantScriptError` instead
of being skipped. ``tests/test_grant_matrix_pg.py`` (the CI ``domain-rls``
job) proves the result equals the ACLs a real release leaves.

Every table is assumed to exist (``to_regclass(...) IS NOT NULL`` is true):
the file runs again after the migrations, and that second pass is the one
whose grants serve requests.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLE_FILE = REPO_ROOT / "deploy" / "postgres" / "domain-tenant-roles.sql"

API_ROLE = "marketai_domain_api"
WORKER_ROLE = "marketai_domain_worker"
READONLY_ROLE = "marketai_domain_readonly"

#: Table privileges PostgreSQL knows; ``ALL [PRIVILEGES]`` means all of them.
TABLE_PRIVILEGES = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
)
#: Privileges that exist only on objects other than tables.
_OTHER_PRIVILEGES = frozenset({"EXECUTE", "USAGE", "CONNECT", "TEMPORARY", "TEMP", "CREATE"})
#: ``GRANT ... ON <kind>`` targets that are not tables.
_NON_TABLE_TARGETS = frozenset(
    {"DATABASE", "SCHEMA", "SEQUENCE", "FUNCTION", "PROCEDURE", "ROUTINE", "LANGUAGE",
     "TYPE", "DOMAIN", "FOREIGN", "LARGE", "TABLESPACE", "PARAMETER"}
)


class GrantScriptError(ValueError):
    """The role file uses SQL this evaluator cannot place."""


@dataclass
class TablePrivileges:
    """One role's privileges on one table: whole-table and per-column."""

    table: set[str] = field(default_factory=set)
    columns: dict[str, set[str]] = field(default_factory=dict)

    def allows(self, privilege: str, columns: frozenset[str] | None) -> bool:
        """Whether ``privilege`` on ``columns`` (None: the statement names no
        column list, so a whole-table privilege is needed) is granted."""
        if privilege in self.table:
            return True
        if not columns:
            return False
        granted = self.columns.get(privilege, set())
        return columns <= granted

    def allows_any(self, privilege: str) -> bool:
        """Whether ``privilege`` is granted on the table or on at least one
        column: what ``SELECT ... FOR UPDATE/SHARE`` needs of UPDATE."""
        return privilege in self.table or bool(self.columns.get(privilege))


@dataclass
class GrantMatrix:
    """``role -> table -> privileges`` after the whole file has run."""

    roles: dict[str, dict[str, TablePrivileges]] = field(default_factory=dict)

    def privileges(self, role: str, table: str) -> TablePrivileges:
        return self.roles.get(role, {}).get(table, TablePrivileges())

    def allows(
        self, role: str, table: str, privilege: str, columns: frozenset[str] | None = None
    ) -> bool:
        return self.privileges(role, table).allows(privilege, columns)

    def acl(self, roles: set[str] | None = None) -> set[tuple[str, str, str, str | None]]:
        """Every grant as ``(role, table, privilege, column or None)``: the
        shape ``tests/test_grant_matrix_pg.py`` reads from ``pg_class`` and
        ``pg_attribute``."""
        rows: set[tuple[str, str, str, str | None]] = set()
        for role, tables in self.roles.items():
            if roles is not None and role not in roles:
                continue
            for table, privileges in tables.items():
                rows.update((role, table, privilege, None) for privilege in privileges.table)
                for privilege, columns in privileges.columns.items():
                    rows.update((role, table, privilege, column) for column in columns)
        return rows

    # -- mutation, used by the evaluator --------------------------------

    def _entry(self, role: str, table: str) -> TablePrivileges:
        return self.roles.setdefault(role, {}).setdefault(table, TablePrivileges())

    def grant(self, role: str, table: str, privilege: str, columns: tuple[str, ...]) -> None:
        entry = self._entry(role, table)
        if columns:
            entry.columns.setdefault(privilege, set()).update(columns)
        else:
            entry.table.add(privilege)

    def revoke(self, role: str, table: str | None, privilege: str | None) -> None:
        """Revoke ``privilege`` (None: all) on ``table`` (None: every table).
        A table-level revoke also revokes the column privileges, as in
        PostgreSQL."""
        tables = self.roles.get(role, {})
        for name in list(tables) if table is None else [table]:
            entry = tables.get(name)
            if entry is None:
                continue
            if privilege is None:
                entry.table.clear()
                entry.columns.clear()
            else:
                entry.table.discard(privilege)
                entry.columns.pop(privilege, None)


# ------------------------------------------------------------------ lexer --

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>--[^\n]*)
  | (?P<dollar>\$(?P<tag>[A-Za-z_]*)\$)
  | (?P<string>'(?:[^']|'')*')
  | (?P<psqlvar>:'?[A-Za-z_][A-Za-z0-9_]*'?)
  | (?P<number>\d+)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")
  | (?P<op><>|!=|\|\||::|[(),;\[\].=<>*+\-/%])
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Token:
    kind: str  # "ident", "string", "number", "op", "body", "psqlvar"
    value: str

    def is_word(self, *words: str) -> bool:
        return self.kind == "ident" and self.value.upper() in words


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    position = 0
    while position < len(sql):
        match = _TOKEN.match(sql, position)
        if match is None:
            raise GrantScriptError(f"cannot read the role file at: {sql[position:position + 40]!r}")
        position = match.end()
        kind = match.lastgroup
        if kind in {"ws", "comment"}:
            continue
        if kind in {"dollar", "tag"}:
            closing = match.group("dollar")
            end = sql.find(closing, position)
            if end < 0:
                raise GrantScriptError(f"unterminated dollar quote {closing}")
            tokens.append(Token("body", sql[position:end]))
            position = end + len(closing)
            continue
        text = match.group(0)
        if kind == "string":
            tokens.append(Token("string", text[1:-1].replace("''", "'")))
        elif kind == "ident":
            if text.startswith('"'):
                tokens.append(Token("ident", text[1:-1].replace('""', '"')))
            else:
                tokens.append(Token("ident", text.lower()))
        else:
            tokens.append(Token(kind, text))
    return tokens


def _split(tokens: list[Token], separator: str = ";") -> Iterator[list[Token]]:
    """Statements (or list items) at parenthesis/bracket depth 0."""
    depth = 0
    current: list[Token] = []
    for token in tokens:
        if token.kind == "op" and token.value in "([":
            depth += 1
        elif token.kind == "op" and token.value in ")]":
            depth -= 1
        if depth == 0 and token.kind == "op" and token.value == separator:
            yield current
            current = []
        else:
            current.append(token)
    if current:
        yield current


# ------------------------------------------------- GRANT / REVOKE --------

def _identifier(tokens: list[Token], index: int) -> tuple[str, int]:
    """``[schema.]name`` starting at ``index``: (name, next index)."""
    if tokens[index].kind != "ident":
        raise GrantScriptError(f"expected a name, found {tokens[index].value!r}")
    name = tokens[index].value
    index += 1
    if index < len(tokens) and tokens[index].kind == "op" and tokens[index].value == ".":
        schema = name
        if schema.lower() != "public":
            raise GrantScriptError(f"grant outside the public schema: {schema}")
        name = tokens[index + 1].value
        index += 2
    return name.lower(), index


def _privileges(tokens: list[Token]) -> list[tuple[str, tuple[str, ...]]]:
    """``SELECT (a, b), INSERT`` -> [(SELECT, (a, b)), (INSERT, ())]."""
    result: list[tuple[str, tuple[str, ...]]] = []
    for item in _split(tokens, ","):
        if not item or item[0].kind != "ident":
            raise GrantScriptError(f"cannot read privilege list {[t.value for t in tokens]}")
        word = item[0].value.upper()
        rest = item[1:]
        if word == "ALL":
            if rest and rest[0].is_word("PRIVILEGES"):
                rest = rest[1:]
            if rest:
                raise GrantScriptError("column lists on ALL are not supported")
            result.extend((privilege, ()) for privilege in sorted(TABLE_PRIVILEGES))
            continue
        columns: tuple[str, ...] = ()
        if rest:
            if not (rest[0].value == "(" and rest[-1].value == ")"):
                raise GrantScriptError(f"cannot read privilege {word} {[t.value for t in rest]}")
            columns = tuple(t.value.lower() for t in rest[1:-1] if t.kind == "ident")
        result.append((word, columns))
    return result


def _roles(tokens: list[Token]) -> list[str]:
    roles = []
    for item in _split(tokens, ","):
        if len(item) != 1 or item[0].kind != "ident":
            raise GrantScriptError(f"cannot read role list {[t.value for t in tokens]}")
        roles.append(item[0].value.lower())
    return roles


def apply_privilege_statement(matrix: GrantMatrix, tokens: list[Token]) -> None:
    """Apply one ``GRANT`` or ``REVOKE`` to ``matrix``."""
    verb = tokens[0].value.upper()
    words = [t for t in tokens]
    on_index = next((i for i, t in enumerate(words) if t.is_word("ON")), None)
    preposition = "TO" if verb == "GRANT" else "FROM"
    to_index = next(
        (i for i, t in enumerate(words) if t.is_word(preposition) and (on_index is None or i > on_index)),
        None,
    )
    if to_index is None:
        raise GrantScriptError(f"{verb} without {preposition}: {' '.join(t.value for t in tokens)}")
    grantee_tokens = words[to_index + 1:]
    # WITH GRANT OPTION / CASCADE / RESTRICT / GRANTED BY tails.
    for stop, token in enumerate(grantee_tokens):
        if token.is_word("WITH", "CASCADE", "RESTRICT", "GRANTED"):
            grantee_tokens = grantee_tokens[:stop]
            break
    roles = _roles(grantee_tokens)

    if on_index is None:
        # Role membership: GRANT role TO role / REVOKE role FROM role.
        if verb == "GRANT":
            raise GrantScriptError(
                "role membership grants change what a runtime role may do and "
                "are not modelled: " + " ".join(t.value for t in tokens)
            )
        return

    privilege_tokens = words[1:on_index]
    if verb == "REVOKE" and privilege_tokens and privilege_tokens[0].is_word("GRANT"):
        raise GrantScriptError("REVOKE GRANT OPTION FOR is not supported")
    target = words[on_index + 1:to_index]
    if not target:
        raise GrantScriptError(f"{verb} without a target")

    first = target[0].value.upper() if target[0].kind == "ident" else ""
    if first in _NON_TABLE_TARGETS:
        return
    if first == "ALL":
        # ON ALL TABLES IN SCHEMA public / ON ALL SEQUENCES / ON ALL FUNCTIONS.
        kind = target[1].value.upper()
        if kind != "TABLES":
            return
        schema = target[-1].value.lower()
        if schema != "public":
            raise GrantScriptError(f"{verb} on schema {schema}")
        if verb == "GRANT":
            raise GrantScriptError("GRANT ... ON ALL TABLES is not modelled")
        privileges = _privileges(privilege_tokens)
        whole = {name for name, _ in privileges} >= TABLE_PRIVILEGES
        for role in roles:
            if whole:
                matrix.revoke(role, None, None)
            else:
                for name, _ in privileges:
                    matrix.revoke(role, None, name)
        return
    if first == "TABLE":
        target = target[1:]

    privileges = _privileges(privilege_tokens)
    unknown = {name for name, _ in privileges} - TABLE_PRIVILEGES
    if unknown & _OTHER_PRIVILEGES:
        raise GrantScriptError(f"{sorted(unknown)} on a table: {' '.join(t.value for t in tokens)}")
    if unknown:
        raise GrantScriptError(f"unknown privileges {sorted(unknown)}")

    tables = []
    index = 0
    while index < len(target):
        name, index = _identifier(target, index)
        tables.append(name)
        if index < len(target):
            if target[index].value != ",":
                raise GrantScriptError(f"cannot read table list {[t.value for t in target]}")
            index += 1

    for role in roles:
        for table in tables:
            for name, columns in privileges:
                if verb == "GRANT":
                    matrix.grant(role, table, name, columns)
                elif columns:
                    raise GrantScriptError("column-level REVOKE is not modelled")
                else:
                    matrix.revoke(role, table, name)


# --------------------------------------------------- PL/pgSQL subset -----

class _Interpreter:
    """Runs the DO blocks' PL/pgSQL: DECLARE, BEGIN/END, FOREACH [SLICE 1],
    IF/ELSE, EXECUTE and static GRANT/REVOKE."""

    def __init__(self, matrix: GrantMatrix) -> None:
        self.matrix = matrix

    # expressions --------------------------------------------------------

    def _expression(self, tokens: list[Token], env: dict[str, object]) -> object:
        parts = [self._term(part, env) for part in _split_concat(tokens)]
        if len(parts) == 1:
            return parts[0]
        if any(part is None for part in parts):
            return None
        return "".join(str(part) for part in parts)

    def _term(self, tokens: list[Token], env: dict[str, object]) -> object:
        if not tokens:
            raise GrantScriptError("empty expression")
        head = tokens[0]
        if len(tokens) == 1 and head.kind == "string":
            return head.value
        if head.kind == "op" and head.value == "(" and tokens[-1].value == ")":
            return self._expression(tokens[1:-1], env)
        if head.is_word("ARRAY") and tokens[1].value == "[" and tokens[-1].value == "]":
            return [self._expression(item, env) for item in _split(tokens[2:-1], ",")]
        if head.kind == "ident" and len(tokens) >= 3 and tokens[1].value == "(" and tokens[-1].value == ")":
            arguments = [self._expression(item, env) for item in _split(tokens[2:-1], ",")]
            return self._call(head.value.lower(), arguments)
        if head.kind == "ident" and head.value in env:
            value = env[head.value]
            if len(tokens) == 1:
                return value
            if tokens[1].value == "[" and tokens[-1].value == "]" and len(tokens) == 4:
                if not isinstance(value, list):
                    raise GrantScriptError(f"{head.value} is not an array")
                return value[int(tokens[2].value) - 1]
        raise GrantScriptError(f"cannot evaluate {' '.join(t.value for t in tokens)}")

    @staticmethod
    def _call(name: str, arguments: list[object]) -> object:
        if name in {"to_regclass", "to_regprocedure"}:
            # Every relation and function the file names exists by the time
            # the post-migration pass runs.
            return arguments[0]
        if name == "format":
            return _format(str(arguments[0]), arguments[1:])
        raise GrantScriptError(f"function {name}() is not modelled")

    def _condition(self, tokens: list[Token], env: dict[str, object]) -> bool:
        words = [t.value.upper() if t.kind == "ident" else t.value for t in tokens]
        if words[-2:] == ["NOT", "NULL"] and words[-3] == "IS":
            return self._expression(tokens[:-3], env) is not None
        if words[-1] == "NULL" and words[-2] == "IS":
            return self._expression(tokens[:-2], env) is None
        for operator in ("<>", "!=", "="):
            if operator in words:
                split = words.index(operator)
                left = self._expression(tokens[:split], env)
                right = self._expression(tokens[split + 1:], env)
                return (left == right) if operator == "=" else (left != right)
        raise GrantScriptError(f"cannot evaluate condition {' '.join(words)}")

    # statements ---------------------------------------------------------

    def run_block(self, body: str) -> None:
        tokens = tokenize(body)
        index = self._block(tokens, 0, {})
        if index < len(tokens) and not (tokens[index].value == ";" and index == len(tokens) - 1):
            raise GrantScriptError("text after the DO block's END")

    def _block(self, tokens: list[Token], index: int, env: dict[str, object]) -> int:
        """``[DECLARE ...] BEGIN stmts END``; returns the index after END."""
        env = dict(env)
        if tokens[index].is_word("DECLARE"):
            index += 1
            while not tokens[index].is_word("BEGIN"):
                end = _find(tokens, index, ";")
                declaration = tokens[index:end]
                env[declaration[0].value] = None
                index = end + 1
        if not tokens[index].is_word("BEGIN"):
            raise GrantScriptError(f"expected BEGIN, found {tokens[index].value!r}")
        index = self._statements(tokens, index + 1, env, ("END",))
        return index + 1  # past END

    def _statements(
        self, tokens: list[Token], index: int, env: dict[str, object], terminators: tuple[str, ...]
    ) -> int:
        """Run statements until one of ``terminators`` (not consumed)."""
        while True:
            token = tokens[index]
            if token.kind == "ident" and token.value.upper() in terminators:
                # END LOOP / END IF are statements of their own enclosing
                # construct; a bare END closes a block.
                return index
            if token.is_word("DECLARE", "BEGIN"):
                index = self._block(tokens, index, env)
                index = _expect(tokens, index, ";")
            elif token.is_word("FOREACH"):
                index = self._foreach(tokens, index, env)
            elif token.is_word("IF"):
                index = self._if(tokens, index, env)
            elif token.is_word("EXECUTE"):
                end = _find(tokens, index, ";")
                statement = self._expression(tokens[index + 1:end], env)
                if not isinstance(statement, str):
                    raise GrantScriptError("EXECUTE of a non-string")
                run_statement(self.matrix, tokenize(statement), self)
                index = end + 1
            elif token.is_word("GRANT", "REVOKE"):
                end = _find(tokens, index, ";")
                apply_privilege_statement(self.matrix, tokens[index:end])
                index = end + 1
            elif token.is_word("NULL"):
                index = _expect(tokens, index + 1, ";")
            else:
                raise GrantScriptError(f"PL/pgSQL statement {token.value!r} is not modelled")

    def _foreach(self, tokens: list[Token], index: int, env: dict[str, object]) -> int:
        variable = tokens[index + 1].value
        index += 2
        slice_depth = 0
        if tokens[index].is_word("SLICE"):
            slice_depth = int(tokens[index + 1].value)
            index += 2
        if not (tokens[index].is_word("IN") and tokens[index + 1].is_word("ARRAY")):
            raise GrantScriptError("expected FOREACH ... IN ARRAY")
        loop = next(i for i in range(index, len(tokens)) if tokens[i].is_word("LOOP"))
        values = self._expression(tokens[index + 2:loop], env)
        if not isinstance(values, list):
            raise GrantScriptError("FOREACH over a non-array")
        nested = any(isinstance(value, list) for value in values)
        if nested != (slice_depth == 1) or slice_depth > 1:
            raise GrantScriptError("FOREACH needs SLICE 1 exactly when the array is 2-D")
        body_start = loop + 1
        end = _skip_to(tokens, body_start, ("END",))
        for value in values:
            scope = dict(env)
            scope[variable] = value
            end = self._statements(tokens, body_start, scope, ("END",))
        if not tokens[end + 1].is_word("LOOP"):
            raise GrantScriptError("expected END LOOP")
        return _expect(tokens, end + 2, ";")

    def _if(self, tokens: list[Token], index: int, env: dict[str, object]) -> int:
        then = next(i for i in range(index, len(tokens)) if tokens[i].is_word("THEN"))
        taken = self._condition(tokens[index + 1:then], env)
        if taken:
            stop = self._statements(tokens, then + 1, env, ("END", "ELSE", "ELSIF"))
        else:
            stop = _skip_to(tokens, then + 1, ("END", "ELSE", "ELSIF"))
        if tokens[stop].is_word("ELSIF"):
            raise GrantScriptError("ELSIF is not modelled")
        if tokens[stop].is_word("ELSE"):
            if taken:
                stop = _skip_to(tokens, stop + 1, ("END",))
            else:
                stop = self._statements(tokens, stop + 1, env, ("END",))
        if not tokens[stop + 1].is_word("IF"):
            raise GrantScriptError("expected END IF")
        return _expect(tokens, stop + 2, ";")


def _split_concat(tokens: list[Token]) -> list[list[Token]]:
    parts: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.kind == "op" and token.value in "([":
            depth += 1
        elif token.kind == "op" and token.value in ")]":
            depth -= 1
        if depth == 0 and token.kind == "op" and token.value == "||":
            parts.append([])
        else:
            parts[-1].append(token)
    return parts


def _find(tokens: list[Token], index: int, value: str) -> int:
    depth = 0
    for position in range(index, len(tokens)):
        token = tokens[position]
        if token.kind == "op" and token.value in "([":
            depth += 1
        elif token.kind == "op" and token.value in ")]":
            depth -= 1
        elif depth == 0 and token.kind == "op" and token.value == value:
            return position
    raise GrantScriptError(f"missing {value!r}")


def _expect(tokens: list[Token], index: int, value: str) -> int:
    if index >= len(tokens) or tokens[index].value != value:
        found = tokens[index].value if index < len(tokens) else "end of block"
        raise GrantScriptError(f"expected {value!r}, found {found!r}")
    return index + 1


def _skip_to(tokens: list[Token], index: int, terminators: tuple[str, ...]) -> int:
    """Index of the first of ``terminators`` at this nesting level, running
    nothing (the branch of an IF that is not taken)."""
    depth = 0
    position = index
    while position < len(tokens):
        token = tokens[position]
        if token.kind == "ident":
            word = token.value.upper()
            if word == "END":
                if depth == 0:
                    if "END" in terminators:
                        return position
                    raise GrantScriptError("unbalanced END")
                depth -= 1
                following = tokens[position + 1] if position + 1 < len(tokens) else None
                position += 2 if following is not None and following.is_word("IF", "LOOP", "CASE") else 1
                continue
            if depth == 0 and word in terminators:
                return position
            if word in {"IF", "LOOP", "BEGIN", "CASE"}:
                depth += 1
        position += 1
    raise GrantScriptError("unterminated block")


def _format(template: str, arguments: list[object]) -> str:
    output = []
    remaining = list(arguments)
    index = 0
    while index < len(template):
        character = template[index]
        if character != "%":
            output.append(character)
            index += 1
            continue
        spec = template[index + 1]
        index += 2
        if spec == "%":
            output.append("%")
            continue
        if not remaining:
            raise GrantScriptError(f"format() is missing an argument for {template!r}")
        value = remaining.pop(0)
        if spec == "I":
            text = str(value)
            output.append(text if re.fullmatch(r"[a-z_][a-z0-9_$]*", text) else '"' + text.replace('"', '""') + '"')
        elif spec == "s":
            output.append("" if value is None else str(value))
        elif spec == "L":
            output.append("NULL" if value is None else "'" + str(value).replace("'", "''") + "'")
        else:
            raise GrantScriptError(f"format() specifier %{spec} is not modelled")
    return "".join(output)


# ------------------------------------------------------------- top level --

_STATEMENT_TERMINATING_META = {"\\gexec", "\\gset", "\\g"}
_IGNORED_META = {"\\set", "\\connect", "\\c", "\\if", "\\endif", "\\echo", "\\unset", "\\pset"}


def _script_statements(sql: str) -> Iterator[list[Token]]:
    """Top-level SQL statements, with psql meta-commands handled: ``\\gexec``
    and ``\\gset`` end the query before them; ``\\if`` blocks are taken."""
    buffer: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("\\"):
            command = stripped.split()[0]
            if command in _STATEMENT_TERMINATING_META:
                buffer.append(";")
            elif command in {"\\else", "\\elif"}:
                raise GrantScriptError(f"psql {command} is not modelled")
            elif command not in _IGNORED_META:
                raise GrantScriptError(f"psql meta-command {command} is not modelled")
            continue
        buffer.append(line)
    for statement in _split(tokenize("\n".join(buffer))):
        if statement:
            yield statement


def run_statement(matrix: GrantMatrix, tokens: list[Token], interpreter: _Interpreter) -> None:
    head = tokens[0]
    if head.is_word("GRANT", "REVOKE"):
        apply_privilege_statement(matrix, tokens)
        return
    if head.is_word("DO"):
        bodies = [t for t in tokens if t.kind == "body"]
        if len(bodies) != 1:
            raise GrantScriptError("DO without a dollar-quoted body")
        interpreter.run_block(bodies[0].value)
        return
    if head.is_word("ALTER") and len(tokens) > 2 and tokens[1].is_word("DEFAULT"):
        # Default privileges apply to tables created later; the release
        # re-runs this file after the migrations, which revokes and
        # re-grants every existing table explicitly.
        return
    # Everything else (roles, settings, the context key) must not hide a
    # privilege change from this evaluator.
    for token in tokens:
        text = token.value.upper()
        if token.is_word("GRANT", "REVOKE") or (
            token.kind in {"string", "body"} and re.search(r"\b(GRANT|REVOKE)\b", text)
        ):
            raise GrantScriptError(
                "a privilege change outside a GRANT/REVOKE/DO statement: "
                + " ".join(t.value for t in tokens)[:200]
            )


def evaluate(sql: str) -> GrantMatrix:
    matrix = GrantMatrix()
    interpreter = _Interpreter(matrix)
    for statement in _script_statements(sql):
        run_statement(matrix, statement, interpreter)
    return matrix


def load_grant_matrix(path: Path = ROLE_FILE) -> GrantMatrix:
    return evaluate(path.read_text(encoding="utf-8"))
