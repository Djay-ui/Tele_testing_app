"""Engine-name -> connector/introspector dispatch, extracted out of
tgdatabridge/gui/main_window.py so it's usable from a GUI-independent context
too -- specifically the headless CLI (tgdatabridge/cli/runner.py, see
ENTERPRISE_READINESS.md section 5, item 1). This module must never import
anything from tgdatabridge.gui or PySide6: the whole point of a headless mode is
that it works on a machine (a CI/CD runner, a server) with no Qt
installed at all.

Previously this dispatch logic lived as two module-level functions inside
main_window.py; moving it here removes the duplication risk of the GUI
and CLI drifting out of sync on which connector class backs which engine
name, and lets both simply import from one place.
"""
from __future__ import annotations

from typing import Callable

from tgdatabridge.db.base import ConnectionParams, DBConnector
from tgdatabridge.db.oracle_connector import OracleConnector

# The engine names as they appear throughout the GUI's combo boxes and
# CLI config files -- kept here as the one canonical list so validation
# (tgdatabridge/cli/config.py) and dispatch never drift apart. Note the source
# list is one longer than the target list: "Excel/CSV" is source-only,
# since writing a converted schema back out to a spreadsheet isn't
# something this tool does (see tgdatabridge.db.spreadsheet_connector).
SOURCE_ENGINES = ("Oracle", "MySQL", "PostgreSQL", "SQL Server", "DB2", "MongoDB", "Excel/CSV")
TARGET_ENGINES = ("Oracle", "PostgreSQL", "MySQL", "SQL Server", "DB2", "MongoDB")

# Engines whose "connection" is a local file path rather than a network
# service. For these, ConnectionParams.database carries the file path and
# host/port/username/password are all meaningless -- the GUI's connection
# dialog and the CLI's config validation both check this rather than
# hardcoding the engine name in three places.
FILE_SOURCE_ENGINES = ("Excel/CSV",)


def make_target_connector(engine: str, params: ConnectionParams) -> DBConnector:
    """Construct the right target-engine connector for `engine` ("Oracle",
    "PostgreSQL", "MySQL", "SQL Server", "DB2", or "MongoDB")."""
    params = _resolve(params)
    if engine == "Oracle":
        return OracleConnector(params)
    if engine == "PostgreSQL":
        from tgdatabridge.db.postgres_connector import PostgresConnector
        return PostgresConnector(params)
    if engine == "SQL Server":
        from tgdatabridge.db.sqlserver_connector import SqlServerConnector
        return SqlServerConnector(params)
    if engine == "DB2":
        from tgdatabridge.db.db2_connector import Db2Connector
        return Db2Connector(params)
    if engine == "MongoDB":
        from tgdatabridge.db.mongo_connector import MongoConnector
        return MongoConnector(params)
    from tgdatabridge.db.mysql_connector import MySQLConnector
    return MySQLConnector(params)


def make_source_connector(engine: str, params: ConnectionParams) -> DBConnector:
    """Construct the right source-engine connector for `engine` ("Oracle",
    "MySQL", "PostgreSQL", "SQL Server", "DB2", "MongoDB" -- every target
    engine can also be a source -- or the source-only "Excel/CSV")."""
    if engine == "Excel/CSV":
        # A local file, so there is nothing to tunnel -- resolved after
        # this branch rather than before it.
        from tgdatabridge.db.spreadsheet_connector import SpreadsheetConnector
        return SpreadsheetConnector(params)
    params = _resolve(params)
    if engine == "MySQL":
        from tgdatabridge.db.mysql_connector import MySQLConnector
        return MySQLConnector(params)
    if engine == "PostgreSQL":
        from tgdatabridge.db.postgres_connector import PostgresConnector
        return PostgresConnector(params)
    if engine == "SQL Server":
        from tgdatabridge.db.sqlserver_connector import SqlServerConnector
        return SqlServerConnector(params)
    if engine == "DB2":
        from tgdatabridge.db.db2_connector import Db2Connector
        return Db2Connector(params)
    if engine == "MongoDB":
        from tgdatabridge.db.mongo_connector import MongoConnector
        return MongoConnector(params)
    return OracleConnector(params)


def _pin_tls_hostname(params: ConnectionParams) -> ConnectionParams:
    """Capture the real database address for certificate hostname
    verification *before* an SSH tunnel (below) rewrites `params.host` to
    127.0.0.1 -- see tgdatabridge/db/tls_config.py's module docstring for
    why this has to happen here, ahead of the tunnel, and nowhere else.

    A no-op for every connection that isn't both TLS-enabled and asking
    for hostname verification: an unencrypted connection has nothing to
    verify, and a connection that already carries an explicit
    `server_host_override` (set by hand, for a case where the
    certificate's name genuinely differs from the address typed into the
    dialog) is left alone rather than overwritten.
    """
    tls = getattr(params, "tls", None)
    if tls is None or not tls.enabled or not tls.verify_hostname:
        return params
    if tls.server_host_override:
        return params
    from dataclasses import replace

    return replace(params, tls=replace(tls, server_host_override=params.host))


def _resolve(params: ConnectionParams) -> ConnectionParams:
    """Open the SSH tunnel this connection asks for, if any, and hand back
    parameters addressed at its local end.

    Every connector in the tool is built through the two factories above,
    so putting this here is what makes "connect through a bastion" work
    for the GUI, the "Test Connection" button and the headless CLI at
    once, without a single connector, introspector or migrator knowing
    that a tunnel exists. A connection with no tunnel configured returns
    the same object it was given.
    """
    from tgdatabridge.db.access import ACCESS_SSH, ACCESS_VPN, check_vpn_route

    tls = getattr(params, "tls", None)
    if tls is not None:
        problem = tls.validate()
        if problem:
            # Surfaced through connector.test_connection()'s own
            # try/except Exception, and through the "Test Connection"
            # button and headless CLI the same way SshTunnelError already
            # is -- a plain, readable message rather than a stack trace.
            raise ValueError(problem)
    params = _pin_tls_hostname(params)
    mode = getattr(params, "access_mode", None)
    if mode == ACCESS_VPN:
        # Nothing to build -- the VPN client already installed the route.
        # Checking it here turns "the driver timed out" into "your VPN is
        # not up", which is the actual problem in almost every case.
        check_vpn_route(params.host, params.port)
        return params
    if getattr(params, "ssh", None) is None:
        return params
    if mode is not None and mode != ACCESS_SSH and not getattr(params.ssh, "enabled", False):
        return params
    from tgdatabridge.db.ssh_tunnel import resolve_params
    return resolve_params(params)


def introspector_for(source_engine: str) -> Callable:
    """Returns the `introspect_schema(conn, schema_name)` function for the
    given source engine -- the same per-engine dispatch main_window.py's
    _load_schema used to inline."""
    if source_engine == "MySQL":
        from tgdatabridge.core.mysql_introspector import introspect_schema
    elif source_engine == "PostgreSQL":
        from tgdatabridge.core.postgres_introspector import introspect_schema
    elif source_engine == "SQL Server":
        from tgdatabridge.core.sqlserver_introspector import introspect_schema
    elif source_engine == "DB2":
        from tgdatabridge.core.db2_introspector import introspect_schema
    elif source_engine == "MongoDB":
        from tgdatabridge.core.mongo_source_introspector import introspect_schema
    elif source_engine == "Excel/CSV":
        from tgdatabridge.core.spreadsheet_introspector import introspect_schema
    else:
        from tgdatabridge.core.introspector import introspect_schema
    return introspect_schema
