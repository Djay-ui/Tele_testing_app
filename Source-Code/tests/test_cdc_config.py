"""Tests for tgdatabridge.cdc.config -- generating a Debezium Oracle connector
configuration from an already-introspected schema (SCALE.md section 2.1).

The property that matters most: the capture include-list is derived from
the same Schema object the migration uses, so a table can't be migrated
by the bulk load and then quietly left uncaptured. A hand-maintained
second copy of that list is exactly how a table ends up diverging from
the source for the whole cutover window with nothing reporting it.

Plain functions, no pytest -- see tests/test_cli_config.py's note.
"""
from tgdatabridge.cdc import config as cdc
from tgdatabridge.core.schema_model import Column, Schema, Table


def _table(name, source_array_path=None):
    table = Table(name=name, schema="HR", source_array_path=source_array_path)
    table.columns = [Column(name="ID", data_type="NUMBER(10)")]
    return table


def _schema(names=("EMPLOYEES", "DEPARTMENTS")):
    schema = Schema(name="HR", source_engine="Oracle")
    schema.tables = [_table(n) for n in names]
    return schema


def _config(**overrides):
    kwargs = dict(
        connector_name="tgdatabridge-cdc",
        database_hostname="dbhost",
        database_port=1521,
        database_user="cdc_user",
        database_dbname="ORCL",
        topic_prefix="hrmig",
        kafka_bootstrap_servers="kafka:9092",
    )
    kwargs.update(overrides)
    return cdc.config_from_schema(_schema(), **kwargs)


# ------------------------------------------------- include list from schema

def test_include_list_comes_from_the_schema():
    rendered = cdc.render_config(_config())
    assert rendered["table.include.list"] == "HR.DEPARTMENTS,HR.EMPLOYEES"


def test_include_list_is_schema_qualified_and_uppercased():
    # Oracle stores unquoted identifiers uppercase; an unqualified or
    # lowercase entry silently matches nothing.
    rendered = cdc.render_config(_config())
    for entry in rendered["table.include.list"].split(","):
        assert entry.startswith("HR.")
        assert entry == entry.upper()


def test_include_list_is_sorted_for_stable_diffs():
    # A generated config that reorders itself between runs makes every
    # regeneration look like a change in review.
    entries = cdc.render_config(_config())["table.include.list"].split(",")
    assert entries == sorted(entries)


def test_table_filter_narrows_the_capture_set():
    # A partial migration must not capture changes for tables it isn't
    # migrating -- those events would have no target table to land in.
    rendered = cdc.render_config(_config(table_names=["EMPLOYEES"]))
    assert rendered["table.include.list"] == "HR.EMPLOYEES"


def test_table_filter_is_case_insensitive():
    rendered = cdc.render_config(_config(table_names=["employees"]))
    assert rendered["table.include.list"] == "HR.EMPLOYEES"


def test_synthesized_child_tables_are_excluded():
    # A MongoDB array-unwind child table has no Oracle table behind it to
    # mine redo for; including it would produce an entry the connector
    # silently never matches.
    schema = _schema()
    schema.tables.append(_table("ORDERS_ITEMS", source_array_path="items"))
    config = cdc.config_from_schema(
        schema, connector_name="c", database_hostname="h", database_port=1521,
        database_user="u", database_dbname="ORCL", topic_prefix="p",
        kafka_bootstrap_servers="k:9092")
    assert "ORDERS_ITEMS" not in cdc.render_config(config)["table.include.list"]


def test_capturable_tables_helper():
    schema = _schema()
    schema.tables.append(_table("CHILD", source_array_path="items"))
    assert [t.name for t in cdc.capturable_tables(schema)] == ["EMPLOYEES", "DEPARTMENTS"]


def test_empty_capture_set_is_an_error_not_an_empty_config():
    # A connector with an empty include list starts cleanly and captures
    # nothing -- the worst possible silent failure here.
    try:
        _config(table_names=["NO_SUCH_TABLE"])
        assert False, "expected CdcConfigError"
    except cdc.CdcConfigError as exc:
        assert "capture nothing" in str(exc)


# ---------------------------------------------------------- core properties

def test_connector_class_is_the_oracle_connector():
    assert cdc.render_config(_config())["connector.class"] == \
        "io.debezium.connector.oracle.OracleConnector"


def test_required_connection_properties_are_present():
    rendered = cdc.render_config(_config())
    for key in ("database.hostname", "database.port", "database.user", "database.password",
                "database.dbname", "topic.prefix",
                "schema.history.internal.kafka.bootstrap.servers",
                "schema.history.internal.kafka.topic"):
        assert key in rendered, key


def test_numeric_properties_are_rendered_as_strings():
    # Kafka Connect's config map is string-to-string; a raw int is
    # rejected by some workers.
    rendered = cdc.render_config(_config())
    assert rendered["database.port"] == "1521"
    assert rendered["tasks.max"] == "1"


def test_pdb_name_is_omitted_for_a_non_cdb_source():
    # Absent is correct for a non-multitenant database, not an oversight.
    assert "database.pdb.name" not in cdc.render_config(_config())


def test_pdb_name_is_included_when_given():
    rendered = cdc.render_config(_config(database_pdb_name="ORCLPDB1"))
    assert rendered["database.pdb.name"] == "ORCLPDB1"


def test_schema_history_topic_defaults_from_the_topic_prefix():
    assert cdc.render_config(_config())["schema.history.internal.kafka.topic"] \
        == "schema-history.hrmig"


def test_schema_history_topic_can_be_overridden():
    rendered = cdc.render_config(_config(schema_history_topic="my-history"))
    assert rendered["schema.history.internal.kafka.topic"] == "my-history"


def test_history_stores_only_captured_tables_ddl():
    # Keeps the history topic proportional to the migration rather than
    # to the entire database.
    assert cdc.render_config(_config())[
        "schema.history.internal.store.only.captured.tables.ddl"] == "true"


# ------------------------------------------------------------- passwords

def test_password_defaults_to_an_env_var_reference():
    # A generated config must be safe to commit next to the pipeline that
    # posts it, exactly like the CLI's own password_env handling.
    rendered = cdc.render_config(_config(password_env="SRC_PW"))
    assert rendered["database.password"] == "${env:SRC_PW}"


def test_password_file_provider_mode():
    rendered = cdc.render_config(_config(password_mode=cdc.PASSWORD_MODE_FILE, password_env="SRC_PW"))
    assert rendered["database.password"].startswith("${file:")
    assert "SRC_PW" in rendered["database.password"]


def test_plain_password_mode_is_available_but_not_the_default():
    rendered = cdc.render_config(_config(password_mode=cdc.PASSWORD_MODE_PLAIN, password_value="s3cret"))
    assert rendered["database.password"] == "s3cret"
    assert cdc.DebeziumConnectorConfig(
        connector_name="c", database_hostname="h", database_port=1, database_user="u",
        database_dbname="d", topic_prefix="p", kafka_bootstrap_servers="k",
    ).password_mode == cdc.PASSWORD_MODE_ENV


def test_unknown_password_mode_is_rejected():
    try:
        cdc.render_config(_config(password_mode="magic"))
        assert False, "expected CdcConfigError"
    except cdc.CdcConfigError as exc:
        assert "password_mode" in str(exc)


# ---------------------------------------------------------- snapshot mode

def test_snapshot_mode_defaults_to_no_data():
    # This tool does the bulk load; Debezium streams only the delta.
    assert cdc.render_config(_config())["snapshot.mode"] == "no_data"


def test_every_documented_snapshot_mode_is_accepted():
    for mode in cdc.SNAPSHOT_MODES:
        assert cdc.render_config(_config(snapshot_mode=mode))["snapshot.mode"] == mode


def test_deprecated_snapshot_spellings_are_rejected():
    # schema_only / schema_only_recovery are deprecated; generating them
    # would produce a warning on every connector start for no benefit.
    for mode in ("schema_only", "schema_only_recovery"):
        try:
            cdc.render_config(_config(snapshot_mode=mode))
            assert False, f"expected {mode} to be rejected"
        except cdc.CdcConfigError:
            pass


def test_unknown_snapshot_mode_is_rejected():
    try:
        cdc.render_config(_config(snapshot_mode="whenever"))
        assert False, "expected CdcConfigError"
    except cdc.CdcConfigError as exc:
        assert "snapshot_mode" in str(exc)


def test_log_mining_strategy_defaults_and_validates():
    assert cdc.render_config(_config())["log.mining.strategy"] == "online_catalog"
    for strategy in cdc.LOG_MINING_STRATEGIES:
        assert cdc.render_config(_config(log_mining_strategy=strategy))["log.mining.strategy"] == strategy
    try:
        cdc.render_config(_config(log_mining_strategy="guess"))
        assert False, "expected CdcConfigError"
    except cdc.CdcConfigError:
        pass


# --------------------------------------------------------- ordering notes

def test_no_data_mode_warns_about_registration_order():
    # The one mistake in this integration that loses data silently.
    notes = " ".join(cdc.ordering_notes(_config(snapshot_mode="no_data")))
    assert "BEFORE starting the bulk load" in notes
    assert "gap" in notes


def test_initial_mode_warns_against_double_loading():
    notes = " ".join(cdc.ordering_notes(_config(snapshot_mode="initial")))
    assert "do NOT also run" in notes
    assert "twice" in notes


def test_tasks_max_above_one_is_flagged_as_ineffective():
    notes = " ".join(cdc.ordering_notes(_config(tasks_max=4)))
    assert "single task" in notes


# ------------------------------------------------------------- rendering

def test_connector_document_has_name_and_config():
    doc = cdc.render_connector_document(_config())
    assert doc["name"] == "tgdatabridge-cdc"
    assert doc["config"]["connector.class"].endswith("OracleConnector")


def test_to_json_round_trips():
    import json
    parsed = json.loads(cdc.to_json(_config()))
    assert parsed["name"] == "tgdatabridge-cdc"
    assert parsed["config"]["topic.prefix"] == "hrmig"


def test_extra_properties_are_merged_last():
    # An escape hatch for anything this module doesn't model, including
    # deliberately overriding something it does.
    rendered = cdc.render_config(_config(extra={"log.mining.log.count.min": "10",
                                                "snapshot.mode": "initial"}))
    assert rendered["log.mining.log.count.min"] == "10"
    assert rendered["snapshot.mode"] == "initial"
