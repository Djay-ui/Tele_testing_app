"""Which real database engines this run can reach, and how to give each
one a disposable namespace to work in.

Discovery is by environment variable, one per engine, each holding a
space-separated ``key=value`` connection string::

    TGSCT_IT_POSTGRES="host=localhost port=5432 dbname=postgres user=postgres password=secret"
    TGSCT_IT_MYSQL="host=127.0.0.1 port=3306 dbname=mysql user=root password=secret"
    TGSCT_IT_SQLSERVER="host=localhost port=1433 dbname=master user=sa password=Secret_123"
    TGSCT_IT_DB2="host=localhost port=50000 dbname=testdb user=db2inst1 password=secret"
    TGSCT_IT_MONGODB="host=localhost port=27017 dbname=admin user= password="

An engine whose variable is unset simply doesn't appear, and every test
parameterised over it skips. That is deliberate: the harness has to be
useful to someone who has PostgreSQL to hand but not Db2, and it must
never be the reason a commit is blocked because a container didn't start.
`docker-compose.integration.yml` in the repository root brings up the
whole set for anyone who wants the full matrix.

Every engine gets a **scratch namespace** created at the start of a test
and dropped at the end, named `tgdatabridge_it_<pid>_<n>`. Nothing outside it is
touched, so this is safe to point at a shared development server -- but
not, obviously, at anything you care about.

The namespace is a schema on PostgreSQL, SQL Server and Db2, and a
database on MySQL and MongoDB, because that is what "a place tables live
that I can drop wholesale" means on each. `Engine.params_for` hides the
difference behind one interface.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from tgdatabridge.db.base import ConnectionParams

# engine label -> environment variable holding its connection string
ENV_VARS = {
    "PostgreSQL": "TGSCT_IT_POSTGRES",
    "MySQL": "TGSCT_IT_MYSQL",
    "SQL Server": "TGSCT_IT_SQLSERVER",
    "DB2": "TGSCT_IT_DB2",
    "MongoDB": "TGSCT_IT_MONGODB",
    "Oracle": "TGSCT_IT_ORACLE",
}


def _parse_dsn(raw: str) -> Dict[str, str]:
    parts: Dict[str, str] = {}
    for chunk in raw.split():
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            parts[key.strip()] = value.strip()
    return parts


@dataclass
class Engine:
    """One reachable engine, plus the two operations the harness needs
    that aren't part of the connector interface: make me somewhere to
    work, and take it away again."""

    label: str
    dsn: Dict[str, str]
    namespace_kind: str          # "schema" | "database"

    # ------------------------------------------------------- connection
    def params_for(self, namespace: str) -> ConnectionParams:
        """ConnectionParams aimed at the scratch namespace."""
        database = namespace if self.namespace_kind == "database" else self.dsn.get("dbname", "")
        schema = namespace if self.namespace_kind == "schema" else None
        return ConnectionParams(
            host=self.dsn.get("host", "localhost"),
            port=int(self.dsn.get("port", 0) or 0),
            database=database,
            username=self.dsn.get("user", ""),
            password=self.dsn.get("password", ""),
            schema=schema,
        )

    def admin_params(self) -> ConnectionParams:
        """ConnectionParams aimed at the *configured* database, used only
        to create and drop scratch namespaces."""
        return ConnectionParams(
            host=self.dsn.get("host", "localhost"),
            port=int(self.dsn.get("port", 0) or 0),
            database=self.dsn.get("dbname", ""),
            username=self.dsn.get("user", ""),
            password=self.dsn.get("password", ""),
            schema=None,
        )

    # -------------------------------------------------- namespace admin
    def create_namespace(self, namespace: str) -> None:
        _NAMESPACE_OPS[self.label][0](self, namespace)

    def drop_namespace(self, namespace: str) -> None:
        try:
            _NAMESPACE_OPS[self.label][1](self, namespace)
        except Exception:  # noqa: BLE001
            # Teardown must never fail a test that already passed. A
            # leaked scratch schema is noise; a spurious red build is not.
            pass


# ------------------------------------------------------------- per engine

def _sql_admin(engine: Engine):
    """A connected admin connector for the engine, for DDL that has to run
    outside the scratch namespace."""
    from tgdatabridge.core.connector_factory import make_target_connector
    conn = make_target_connector(engine.label, engine.admin_params())
    conn.connect()
    return conn


def _pg_create(engine: Engine, ns: str) -> None:
    conn = _sql_admin(engine)
    try:
        conn.execute_ddl(f'CREATE SCHEMA IF NOT EXISTS "{ns}"')
    finally:
        conn.close()


def _pg_drop(engine: Engine, ns: str) -> None:
    conn = _sql_admin(engine)
    try:
        conn.execute_ddl(f'DROP SCHEMA IF EXISTS "{ns}" CASCADE')
    finally:
        conn.close()


def _mysql_create(engine: Engine, ns: str) -> None:
    conn = _sql_admin(engine)
    try:
        conn.execute_ddl(f"CREATE DATABASE IF NOT EXISTS `{ns}`")
    finally:
        conn.close()


def _mysql_drop(engine: Engine, ns: str) -> None:
    conn = _sql_admin(engine)
    try:
        conn.execute_ddl(f"DROP DATABASE IF EXISTS `{ns}`")
    finally:
        conn.close()


def _mssql_create(engine: Engine, ns: str) -> None:
    conn = _sql_admin(engine)
    try:
        conn.execute_ddl(
            f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{ns}') "
            f"EXEC('CREATE SCHEMA [{ns}]')")
    finally:
        conn.close()


def _mssql_drop(engine: Engine, ns: str) -> None:
    conn = _sql_admin(engine)
    try:
        # Every table has to go before the schema will.
        rows = list(conn.execute(
            "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
            f"WHERE s.name = '{ns}'"))
        for (name,) in rows:
            conn.execute_ddl(f"DROP TABLE [{ns}].[{name}]")
        conn.execute_ddl(f"DROP SCHEMA [{ns}]")
    finally:
        conn.close()


def _db2_create(engine: Engine, ns: str) -> None:
    conn = _sql_admin(engine)
    try:
        conn.execute_ddl(f'CREATE SCHEMA "{ns.upper()}"')
    finally:
        conn.close()


def _db2_drop(engine: Engine, ns: str) -> None:
    conn = _sql_admin(engine)
    try:
        rows = list(conn.execute(
            f"SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA = '{ns.upper()}' AND TYPE = 'T'"))
        for (name,) in rows:
            conn.execute_ddl(f'DROP TABLE "{ns.upper()}"."{name}"')
        conn.execute_ddl(f'DROP SCHEMA "{ns.upper()}" RESTRICT')
    finally:
        conn.close()


def _mongo_create(engine: Engine, ns: str) -> None:
    # A MongoDB database springs into existence on first write; nothing to do.
    pass


def _mongo_drop(engine: Engine, ns: str) -> None:
    from tgdatabridge.core.connector_factory import make_target_connector
    conn = make_target_connector(engine.label, engine.params_for(ns))
    conn.connect()
    try:
        conn._conn.client.drop_database(ns)
    finally:
        conn.close()


def _oracle_create(engine: Engine, ns: str) -> None:
    # An Oracle "schema" is a user account, and creating one needs
    # privileges this harness has no business assuming. Oracle therefore
    # runs against whatever schema the DSN's user already owns.
    pass


def _oracle_drop(engine: Engine, ns: str) -> None:
    pass


_NAMESPACE_OPS: Dict[str, tuple] = {
    "PostgreSQL": (_pg_create, _pg_drop),
    "MySQL": (_mysql_create, _mysql_drop),
    "SQL Server": (_mssql_create, _mssql_drop),
    "DB2": (_db2_create, _db2_drop),
    "MongoDB": (_mongo_create, _mongo_drop),
    "Oracle": (_oracle_create, _oracle_drop),
}

_NAMESPACE_KIND = {
    "PostgreSQL": "schema",
    "MySQL": "database",
    "SQL Server": "schema",
    "DB2": "schema",
    "MongoDB": "database",
    "Oracle": "schema",
}


def available_engines() -> List[Engine]:
    """Every engine this run can actually reach, in a stable order so
    parameterised test ids don't shuffle between runs."""
    found: List[Engine] = []
    for label in ("PostgreSQL", "MySQL", "SQL Server", "DB2", "MongoDB", "Oracle"):
        raw = os.environ.get(ENV_VARS[label], "").strip()
        if not raw:
            continue
        found.append(Engine(label=label, dsn=_parse_dsn(raw),
                            namespace_kind=_NAMESPACE_KIND[label]))
    return found


def missing_engines() -> List[str]:
    return [label for label, var in ENV_VARS.items() if not os.environ.get(var, "").strip()]
