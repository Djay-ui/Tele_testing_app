"""Delimiter escaping in generated SQL identifiers.

Every dialect this tool emits delimits identifiers with a character that
can legally appear *inside* an identifier, escaped by doubling it. Before
tgdatabridge.utils.identifiers existed, every quoting site wrapped without
escaping, which was wrong twice over:

* Correctness -- CREATE TABLE "My""Table" is legal Oracle and legal
  PostgreSQL. Unescaped, such a name produced broken DDL.
* Injection -- object names are read from whatever source database the
  tool is pointed at, and the DDL built from them is then *executed
  against the target* by Apply DDL. A table named

      X"; DROP TABLE IMPORTANT; --

  would, unescaped, terminate the quoted identifier and inject arbitrary
  SQL into the generated script.

The whole existing suite passed both before and after that fix, because
nothing exercised an identifier containing a delimiter -- which is
exactly why these tests exist. Each one below fails against the
pre-fix code.
"""
import pytest

from tgdatabridge.core import ddl_generator
from tgdatabridge.core.schema_model import Column, Constraint, Index, Table
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.db2_connector import Db2Connector
from tgdatabridge.db.mysql_connector import MySQLConnector
from tgdatabridge.db.oracle_connector import OracleConnector
from tgdatabridge.db.postgres_connector import PostgresConnector
from tgdatabridge.db.sqlserver_connector import SqlServerConnector
from tgdatabridge.utils.identifiers import quote_backtick, quote_bracket, quote_double

# A name that closes the quoted identifier and appends a second statement.
# Used verbatim as a table/column name throughout the tests below.
DQ_PAYLOAD = 'X"; DROP TABLE IMPORTANT; --'
BT_PAYLOAD = "X`; DROP TABLE IMPORTANT; --"
BR_PAYLOAD = "X]; DROP TABLE IMPORTANT; --"


# --------------------------------------------------------------- primitives

def test_quote_double_doubles_embedded_quote():
    assert quote_double('My"Table') == '"My""Table"'


def test_quote_backtick_doubles_embedded_backtick():
    assert quote_backtick("My`Table") == "`My``Table`"


def test_quote_bracket_doubles_embedded_closing_bracket():
    # Only the closing bracket needs escaping -- an opening bracket inside a
    # bracket-quoted identifier is an ordinary character, so this is
    # deliberately not symmetric with the two above.
    assert quote_bracket("My]Table") == "[My]]Table]"
    assert quote_bracket("My[Table") == "[My[Table]"


def test_quote_helpers_leave_ordinary_identifiers_alone():
    assert quote_double("EMPLOYEES") == '"EMPLOYEES"'
    assert quote_backtick("employees") == "`employees`"
    assert quote_bracket("Employees") == "[Employees]"


@pytest.mark.parametrize("fn,delim", [
    (quote_double, '"'), (quote_backtick, "`"), (quote_bracket, "]"),
])
def test_quoted_result_has_no_unescaped_delimiter_inside(fn, delim):
    """The structural invariant: between the outer delimiters, every
    occurrence of the delimiter character appears an even number of times
    in a row, so nothing can terminate the identifier early."""
    quoted = fn(DQ_PAYLOAD + BT_PAYLOAD + BR_PAYLOAD)
    inner = quoted[1:-1]
    runs, i = [], 0
    while i < len(inner):
        if inner[i] == delim:
            j = i
            while j < len(inner) and inner[j] == delim:
                j += 1
            runs.append(j - i)
            i = j
        else:
            i += 1
    assert all(r % 2 == 0 for r in runs), f"odd-length delimiter run in {quoted!r}"


# ------------------------------------------------------- dialect quoting

def test_each_dialect_helper_escapes_its_own_delimiter():
    assert ddl_generator._quote_pg('My"Table') == '"my""table"'
    assert ddl_generator._quote_mysql("My`Table") == "`My``Table`"
    assert ddl_generator._quote_sqlserver("My]Table") == "[My]]Table]"
    assert ddl_generator._quote_db2('My"Table') == '"MY""TABLE"'
    assert ddl_generator._quote_oracle('My"Table') == '"MY""TABLE"'


def test_schema_qualification_escapes_the_schema_name_too():
    # The schema half of a qualified name went through the same unescaped
    # interpolation as the object half.
    assert ddl_generator._quote_sqlserver("T", schema="My]Schema") == "[My]]Schema].[T]"
    assert ddl_generator._quote_db2("T", schema='My"Schema') == '"MY""SCHEMA"."T"'
    assert ddl_generator._quote_oracle("T", schema='My"Schema') == '"MY""SCHEMA"."T"'


# ------------------------------------------------------------ end to end

def executable_text(sql: str, open_delim: str, close_delim: str = None) -> str:
    """Strip quoted identifiers and single-quoted string literals from
    `sql`, leaving only text the engine would treat as SQL syntax.

    This is what makes the end-to-end assertions meaningful. A naive
    "payload must not appear in the DDL" check is wrong in both
    directions: the payload is *supposed* to appear (it is the object's
    real name), and a bare double quote sitting inside a single-quoted
    literal -- which Db2's and Oracle's idempotency guards legitimately
    emit -- is harmless rather than an escape. The only question worth
    asking is whether the payload can ever land where the engine would
    execute it, so that is what this measures.

    Doubling is handled for both delimiter kinds, so a correctly escaped
    identifier is consumed whole.
    """
    close_delim = close_delim or open_delim
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":                       # string literal, '' escapes
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch == open_delim:              # quoted identifier
            i += 1
            while i < n:
                if sql[i] == close_delim:
                    if i + 1 < n and sql[i + 1] == close_delim:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def test_executable_text_helper_is_itself_correct():
    # Guard the guard: if this scanner were broken it could mask a real
    # failure in every test that uses it.
    assert "DROP" not in executable_text('CREATE TABLE "a""; DROP ME" (x INT)', '"')
    assert "DROP" in executable_text('CREATE TABLE "a"; DROP ME', '"')
    assert "DROP" not in executable_text("SELECT 'a''; DROP ME'", '"')
    assert "DROP" not in executable_text("CREATE TABLE [a]]; DROP ME] (x INT)", "[", "]")
    assert "DROP" in executable_text("CREATE TABLE [a]; DROP ME", "[", "]")



def _hostile_table(name_payload: str, col_payload: str) -> Table:
    return Table(
        name=name_payload,
        schema="HR",
        columns=[
            Column(name=col_payload, data_type="NUMBER(9)", nullable=False),
            Column(name="SAFE_COL", data_type="VARCHAR2(50)", nullable=True),
        ],
        constraints=[Constraint(name="PK_" + "X", kind="PRIMARY KEY", columns=[col_payload])],
        indexes=[Index(name="IDX_X", columns=[col_payload], unique=False)],
    )


@pytest.mark.parametrize("generator,payload,open_d,close_d", [
    (ddl_generator.generate_table_ddl_postgres, DQ_PAYLOAD, '"', '"'),
    (ddl_generator.generate_table_ddl_mysql, BT_PAYLOAD, "`", "`"),
    (ddl_generator.generate_table_ddl_sqlserver, BR_PAYLOAD, "[", "]"),
    (ddl_generator.generate_table_ddl_db2, DQ_PAYLOAD, '"', '"'),
    (ddl_generator.generate_table_ddl_oracle, DQ_PAYLOAD, '"', '"'),
])
def test_hostile_object_name_cannot_inject_a_second_statement(generator, payload, open_d, close_d):
    """The end-to-end case this defect was really about: a source object
    named so as to break out of its own quoting, run through real DDL
    generation for each SQL target."""
    ddl, _issues = generator(_hostile_table(payload, payload))
    # The name itself is of course present -- it is the object's real name.
    assert "DROP TABLE IMPORTANT" in ddl.upper()
    # But it must never survive into executable position.
    assert "DROP TABLE IMPORTANT" not in executable_text(ddl, open_d, close_d).upper()


# ------------------------------------------------------------ connectors

class _FakeCopy:
    """psycopg3's cursor.copy() context manager -- PostgresConnector
    bulk-loads via COPY rather than executemany (SCALE.md 1.1), so its
    identifier quoting lives on a different code path to every other
    connector's and needs its own stand-in here."""

    def __init__(self, sink_rows):
        self.sink_rows = sink_rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def write_row(self, row):
        self.sink_rows.append(tuple(row))


class _FakeCursor:
    def __init__(self):
        self.executed = []
        self.executemany_calls = []
        self.copied_rows = []

    def execute(self, sql, args=None):
        self.executed.append((sql, args))

    def executemany(self, sql, rows):
        self.executemany_calls.append((sql, rows))

    def copy(self, sql):
        # Recorded in executemany_calls too, so the shared assertion below
        # can treat COPY and INSERT uniformly as "the statement issued".
        self.executemany_calls.append((sql, []))
        return _FakeCopy(self.copied_rows)

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.cursor_obj = _FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        pass


def _params(**over):
    base = dict(host="h", port=1, database="d", username="u", password="p", schema=None)
    base.update(over)
    return ConnectionParams(**base)


@pytest.mark.parametrize("cls,payload,open_d,close_d", [
    (OracleConnector, DQ_PAYLOAD, '"', '"'),
    (Db2Connector, DQ_PAYLOAD, '"', '"'),
    (PostgresConnector, DQ_PAYLOAD, '"', '"'),
    (MySQLConnector, BT_PAYLOAD, "`", "`"),
    (SqlServerConnector, BR_PAYLOAD, "[", "]"),
])
def test_insert_batch_escapes_hostile_table_and_column_names(cls, payload, open_d, close_d):
    """insert_batch builds its own SQL string rather than going through
    ddl_generator, so it needed the same fix and needs its own test."""
    conn = cls(_params())
    conn._conn = _FakeConn()
    conn.insert_batch(payload, [payload, "SAFE"], [(1, "a")])
    calls = conn._conn.cursor_obj.executemany_calls
    assert calls, f"{cls.__name__}.insert_batch issued no statement"
    sql = calls[0][0]
    assert "DROP TABLE IMPORTANT" not in executable_text(sql, open_d, close_d).upper()


@pytest.mark.parametrize("cls,payload,open_d,close_d", [
    (OracleConnector, DQ_PAYLOAD, '"', '"'),
    (Db2Connector, DQ_PAYLOAD, '"', '"'),
    (PostgresConnector, DQ_PAYLOAD, '"', '"'),
    (MySQLConnector, BT_PAYLOAD, "`", "`"),
    (SqlServerConnector, BR_PAYLOAD, "[", "]"),
])
def test_count_rows_escapes_hostile_table_name(cls, payload, open_d, close_d):
    """count_rows runs during post-migration validation, against the same
    attacker-influenced table name."""
    conn = cls(_params())
    conn._conn = _FakeConn()
    conn.count_rows(payload)
    executed = conn._conn.cursor_obj.executed
    assert executed, f"{cls.__name__}.count_rows issued no statement"
    sql = executed[0][0]
    assert "DROP TABLE IMPORTANT" not in executable_text(sql, open_d, close_d).upper()


# --------------------------------------------- schema preamble & MongoDB

def test_postgres_schema_preamble_escapes_the_target_schema_name():
    """The CREATE SCHEMA / SET search_path preamble interpolated the
    user-supplied target schema straight into a quoted identifier."""
    from tgdatabridge.core.schema_model import Schema
    schema = Schema(name="HR", tables=[], views=[], sequences=[], routines=[])
    ddl, _ = ddl_generator.generate_schema_ddl(
        schema, "postgresql", target_schema='ev"il')
    assert '"ev""il"' in ddl
    assert "ev\"il" not in executable_text(ddl, '"')


def test_mongodb_drop_json_encodes_the_collection_name():
    """mongo_connector parses this JS back with json.loads, so every name
    must be json.dumps-encoded -- this emitter used a bare f-string, which
    would have produced unparseable JS for a name containing a quote."""
    import json as _json
    from tgdatabridge.core.schema_model import Table
    table = Table(name='ev"il', schema="HR", columns=[], constraints=[], indexes=[])
    stmt = ddl_generator._drop_table_ddl(table, "mongodb")
    assert stmt == 'db["ev\\"il"].drop();'
    # And the encoded name really does round-trip back through json.loads,
    # which is what the connector actually does with it.
    inner = stmt[len("db["):stmt.index("].drop();")]
    assert _json.loads(inner) == 'ev"il'
