"""Tests for tgdatabridge.core.connector_factory -- the GUI-independent engine
dispatch used by both the GUI (tgdatabridge/gui/main_window.py) and the headless
CLI (tgdatabridge/cli/runner.py). This module must be importable and usable with
zero PySide6 dependency, since that's the entire point of it existing
separately from main_window.py."""
import pytest

from tgdatabridge.core import connector_factory as cf
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.tls_config import TlsConfig


def _params():
    return ConnectionParams(host="h", port=1, database="d", username="u", password="p")


def test_make_source_connector_dispatches_every_engine():
    expected = {
        "Oracle": "OracleConnector",
        "MySQL": "MySQLConnector",
        "PostgreSQL": "PostgresConnector",
        "SQL Server": "SqlServerConnector",
        "DB2": "Db2Connector",
        "MongoDB": "MongoConnector",
    }
    for engine, class_name in expected.items():
        conn = cf.make_source_connector(engine, _params())
        assert type(conn).__name__ == class_name, f"{engine} -> {type(conn).__name__}"


def test_make_target_connector_dispatches_every_engine():
    expected = {
        "Oracle": "OracleConnector",
        "PostgreSQL": "PostgresConnector",
        "MySQL": "MySQLConnector",
        "SQL Server": "SqlServerConnector",
        "DB2": "Db2Connector",
        "MongoDB": "MongoConnector",
    }
    for engine, class_name in expected.items():
        conn = cf.make_target_connector(engine, _params())
        assert type(conn).__name__ == class_name, f"{engine} -> {type(conn).__name__}"


def test_unknown_engine_falls_back_to_oracle():
    # Matches the pre-existing main_window.py dispatch behavior this was
    # extracted from -- an unrecognized string falls through to Oracle
    # rather than raising, since the GUI combo boxes only ever offer the
    # six known engines and this is meant to be a last-resort default, not
    # a validation point (tgdatabridge.cli.config does the real validation for
    # the CLI's config files).
    assert type(cf.make_source_connector("Nonsense", _params())).__name__ == "OracleConnector"
    assert type(cf.make_target_connector("Nonsense", _params())).__name__ == "MySQLConnector"


def test_introspector_for_every_source_engine_returns_callable():
    for engine in cf.SOURCE_ENGINES:
        fn = cf.introspector_for(engine)
        assert callable(fn)


def test_engine_lists_match_what_the_gui_and_cli_both_expect():
    assert cf.SOURCE_ENGINES == (
        "Oracle", "MySQL", "PostgreSQL", "SQL Server", "DB2", "MongoDB", "Excel/CSV")
    assert cf.TARGET_ENGINES == ("Oracle", "PostgreSQL", "MySQL", "SQL Server", "DB2", "MongoDB")


def test_excel_csv_is_source_only():
    # Writing a converted schema back out to a spreadsheet isn't something
    # this tool does, so it must never appear as a target option.
    assert "Excel/CSV" in cf.SOURCE_ENGINES
    assert "Excel/CSV" not in cf.TARGET_ENGINES


def test_file_source_engines_are_a_subset_of_source_engines():
    assert set(cf.FILE_SOURCE_ENGINES) <= set(cf.SOURCE_ENGINES)
    assert cf.FILE_SOURCE_ENGINES == ("Excel/CSV",)


def test_make_source_connector_dispatches_excel_csv():
    conn = cf.make_source_connector("Excel/CSV", _params())
    assert type(conn).__name__ == "SpreadsheetConnector"


def test_introspector_for_excel_csv():
    fn = cf.introspector_for("Excel/CSV")
    assert fn.__module__ == "tgdatabridge.core.spreadsheet_introspector"


# ------------------------------------------------------------------- TLS

def test_a_connection_with_no_tls_is_untouched():
    params = ConnectionParams(host="db.internal", port=5432, database="app",
                               username="u", password="p")
    resolved = cf._resolve(params)
    assert resolved is params


def test_pin_tls_hostname_captures_the_real_host():
    params = ConnectionParams(host="db.internal", port=5432, database="app",
                               username="u", password="p",
                               tls=TlsConfig(enabled=True, verify_hostname=True))
    pinned = cf._pin_tls_hostname(params)
    assert pinned.tls.server_host_override == "db.internal"
    # And the original is not mutated.
    assert params.tls.server_host_override == ""


def test_pin_tls_hostname_leaves_an_explicit_override_alone():
    params = ConnectionParams(
        host="db.internal", port=5432, database="app", username="u", password="p",
        tls=TlsConfig(enabled=True, verify_hostname=True, server_host_override="custom.name"))
    pinned = cf._pin_tls_hostname(params)
    assert pinned.tls.server_host_override == "custom.name"


def test_pin_tls_hostname_is_a_noop_when_hostname_verification_is_off():
    params = ConnectionParams(
        host="db.internal", port=5432, database="app", username="u", password="p",
        tls=TlsConfig(enabled=True, verify_hostname=False))
    pinned = cf._pin_tls_hostname(params)
    assert pinned.tls.server_host_override == ""


def test_pin_tls_hostname_is_a_noop_when_tls_is_disabled():
    params = ConnectionParams(
        host="db.internal", port=5432, database="app", username="u", password="p",
        tls=TlsConfig(enabled=False))
    pinned = cf._pin_tls_hostname(params)
    assert pinned is params


def test_resolve_pins_the_hostname_before_returning():
    params = ConnectionParams(
        host="db.internal", port=5432, database="app", username="u", password="p",
        tls=TlsConfig(enabled=True, verify_hostname=True))
    resolved = cf._resolve(params)
    assert resolved.tls.server_host_override == "db.internal"
    assert resolved.host == "db.internal"  # unchanged: no SSH tunnel here


def test_an_invalid_tls_config_is_rejected_before_a_connector_is_built():
    params = ConnectionParams(
        host="db.internal", port=5432, database="app", username="u", password="p",
        tls=TlsConfig(enabled=True, ca_cert_path="/no/such/file.pem"))
    with pytest.raises(ValueError, match="CA certificate file not found"):
        cf.make_target_connector("PostgreSQL", params)


def test_a_valid_tls_config_builds_the_connector_normally():
    params = ConnectionParams(
        host="db.internal", port=5432, database="app", username="u", password="p",
        tls=TlsConfig(enabled=True))
    conn = cf.make_target_connector("PostgreSQL", params)
    assert type(conn).__name__ == "PostgresConnector"
    assert conn.params.tls.server_host_override == "db.internal"


def test_connector_factory_module_has_no_pyside6_dependency():
    # The whole point of extracting this module out of main_window.py was
    # so the headless CLI never needs Qt installed at all. Guard against a
    # future edit accidentally reintroducing an actual import here (the
    # module's own docstring mentions "PySide6"/"tgdatabridge.gui" by name as
    # explanation, so this checks for real import statements specifically
    # rather than doing a blunt substring search over the whole file).
    lines = open(cf.__file__, encoding="utf-8").read().splitlines()
    import_lines = [l.strip() for l in lines if l.strip().startswith(("import ", "from "))]
    assert not any("pyside6" in l.lower() for l in import_lines)
    assert not any("tgdatabridge.gui" in l for l in import_lines)
