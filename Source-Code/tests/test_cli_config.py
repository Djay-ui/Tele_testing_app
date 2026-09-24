"""Tests for tgdatabridge.cli.config -- config-as-code for the headless CLI
(ENTERPRISE_READINESS.md section 5, item 2). Plain functions, no pytest
(this repo's fixture-free hand-rolled test runner doesn't have pytest
available at all -- see /tmp/run_tests.py) -- expected errors are checked
via try/except, matching every other test module in this repo."""
import json
import os
import pathlib
import shutil
import tempfile

from tgdatabridge.cli.config import (
    CliConfigError, load_job_config, parse_job_config, resolve_connection_params,
)


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="tgdatabridge_test_"))


def _valid_raw(**overrides):
    raw = {
        "source": {
            "engine": "Oracle", "host": "srchost", "port": 1521, "database": "orcl",
            "username": "hr_admin", "password_env": "SRC_PW", "schema": "HR",
        },
        "target": {
            "engine": "PostgreSQL", "host": "tgthost", "port": 5432, "database": "hrdb",
            "username": "postgres", "password_env": "TGT_PW",
        },
        "target_schema": "public",
    }
    raw.update(overrides)
    return raw


def _expect_config_error(fn, must_contain=None):
    try:
        fn()
    except CliConfigError as exc:
        if must_contain is not None:
            assert must_contain.lower() in str(exc).lower(), f"expected {must_contain!r} in: {exc}"
        return
    raise AssertionError("expected CliConfigError, none was raised")


# ------------------------------------------------------------- parse_job_config


def test_parse_job_config_valid_minimal():
    config = parse_job_config(_valid_raw())
    assert config.source.engine == "Oracle"
    assert config.source.host == "srchost"
    assert config.source.schema == "HR"
    assert config.target.engine == "PostgreSQL"
    assert config.target_schema == "public"
    assert config.apply_ddl is False
    assert config.migrate is False
    assert config.max_workers == 1
    assert config.production is False
    assert config.tables is None


def test_parse_job_config_all_fields():
    raw = _valid_raw(
        tables=["EMPLOYEES", "DEPARTMENTS"], apply_ddl=True, migrate=True,
        dry_run_migrate=True, max_workers=4, production=True,
        approval_command="true", output_dir="/tmp/out",
    )
    config = parse_job_config(raw)
    assert config.tables == ["EMPLOYEES", "DEPARTMENTS"]
    assert config.apply_ddl is True
    assert config.migrate is True
    assert config.dry_run_migrate is True
    assert config.max_workers == 4
    assert config.production is True
    assert config.approval_command == "true"
    assert config.output_dir == "/tmp/out"


def test_parse_job_config_top_level_not_a_dict():
    _expect_config_error(lambda: parse_job_config([1, 2, 3]))


def test_parse_job_config_missing_source_or_target():
    raw = _valid_raw()
    del raw["target"]
    _expect_config_error(lambda: parse_job_config(raw), "target")


def test_parse_job_config_unknown_top_level_field():
    raw = _valid_raw()
    raw["totally_made_up_field"] = 1
    _expect_config_error(lambda: parse_job_config(raw), "unknown top-level field")


def test_parse_job_config_unknown_connection_field():
    raw = _valid_raw()
    raw["source"]["made_up"] = 1
    _expect_config_error(lambda: parse_job_config(raw), "unknown field")


def test_parse_job_config_missing_connection_field():
    raw = _valid_raw()
    del raw["source"]["port"]
    _expect_config_error(lambda: parse_job_config(raw), "missing required field")


def test_parse_job_config_unknown_engine():
    raw = _valid_raw()
    raw["source"]["engine"] = "Nonsense"
    _expect_config_error(lambda: parse_job_config(raw), "Nonsense")


def test_parse_job_config_bad_port():
    raw = _valid_raw()
    raw["source"]["port"] = "not-a-number"
    _expect_config_error(lambda: parse_job_config(raw), "port")


def test_parse_job_config_tables_must_be_list_of_strings():
    _expect_config_error(lambda: parse_job_config(_valid_raw(tables="EMPLOYEES")), "tables")
    _expect_config_error(lambda: parse_job_config(_valid_raw(tables=[1, 2])), "tables")


def test_parse_job_config_bad_max_workers():
    _expect_config_error(lambda: parse_job_config(_valid_raw(max_workers=0)), "max_workers")
    _expect_config_error(lambda: parse_job_config(_valid_raw(max_workers="lots")), "max_workers")


def test_parse_job_config_approval_command_must_be_string():
    _expect_config_error(lambda: parse_job_config(_valid_raw(approval_command=123)), "approval_command")


def test_parse_job_config_source_and_target_can_use_different_engine_sets():
    # e.g. MongoDB can be a source (schema-inference based) or a target
    # (relational-to-document) -- both engine lists include it.
    raw = _valid_raw()
    raw["source"]["engine"] = "MongoDB"
    del raw["source"]["schema"]
    config = parse_job_config(raw)
    assert config.source.engine == "MongoDB"


# ------------------------------------------------------------- load_job_config


def test_load_job_config_json():
    base = _tmp_dir()
    try:
        path = base / "job.json"
        path.write_text(json.dumps(_valid_raw()), encoding="utf-8")
        config = load_job_config(path)
        assert config.source.engine == "Oracle"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_job_config_missing_file():
    _expect_config_error(
        lambda: load_job_config(pathlib.Path("/definitely/does/not/exist/job.json")), "could not read")


def test_load_job_config_malformed_json():
    base = _tmp_dir()
    try:
        path = base / "job.json"
        path.write_text("{not valid json", encoding="utf-8")
        _expect_config_error(lambda: load_job_config(path), "parse")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_job_config_yaml_without_pyyaml_gives_actionable_error():
    try:
        import yaml  # noqa: F401
        return  # PyYAML happens to be installed here -- covered by the success test below instead.
    except ImportError:
        pass

    base = _tmp_dir()
    try:
        path = base / "job.yaml"
        path.write_text("source: {}\ntarget: {}\n", encoding="utf-8")
        _expect_config_error(lambda: load_job_config(path), "pyyaml")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_job_config_yaml_when_pyyaml_available():
    try:
        import yaml
    except ImportError:
        return  # nothing to test in this environment -- covered by the error-path test above instead.

    base = _tmp_dir()
    try:
        path = base / "job.yaml"
        path.write_text(yaml.safe_dump(_valid_raw()), encoding="utf-8")
        config = load_job_config(path)
        assert config.source.engine == "Oracle"
        assert config.target.engine == "PostgreSQL"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ------------------------------------------------------- resolve_connection_params


def test_resolve_connection_params_reads_password_from_env():
    config = parse_job_config(_valid_raw())
    os.environ["SRC_PW"] = "s3cr3t"
    try:
        params = resolve_connection_params(config.source)
        assert params.password == "s3cr3t"
        assert params.host == "srchost"
        assert params.schema == "HR"
    finally:
        os.environ.pop("SRC_PW", None)


def test_resolve_connection_params_missing_env_var_raises():
    config = parse_job_config(_valid_raw())
    os.environ.pop("SRC_PW", None)  # ensure it's really unset
    _expect_config_error(lambda: resolve_connection_params(config.source), "SRC_PW")


def test_resolve_connection_params_never_leaks_password_into_config_object():
    config = parse_job_config(_valid_raw())
    assert not hasattr(config.source, "password")


# ------------------------------------------- file-based source engines (Excel/CSV)


def _excel_raw(**source_overrides):
    source = {"engine": "Excel/CSV", "database": "/data/sales.xlsx"}
    source.update(source_overrides)
    raw = _valid_raw()
    raw["source"] = source
    return raw


def test_excel_source_needs_only_engine_and_database():
    # There's no host to reach, no port to open and no credential to
    # supply, so requiring those would mean putting meaningless
    # placeholders in every spreadsheet job config.
    config = parse_job_config(_excel_raw())
    assert config.source.engine == "Excel/CSV"
    assert config.source.database == "/data/sales.xlsx"
    assert config.source.host == ""
    assert config.source.port == 0
    assert config.source.username == ""
    assert config.source.password_env == ""


def test_excel_source_accepts_an_optional_schema():
    config = parse_job_config(_excel_raw(schema="reporting"))
    assert config.source.schema == "reporting"


def test_excel_source_missing_database_is_an_actionable_error():
    raw = _valid_raw()
    raw["source"] = {"engine": "Excel/CSV"}
    _expect_config_error(lambda: parse_job_config(raw), "database")


def test_excel_source_rejects_fields_that_do_not_apply():
    # Silently ignoring a host/username here would leave someone
    # convinced they'd configured something that does nothing.
    for field_name, value in (("host", "h"), ("port", 1521), ("username", "u"), ("password_env", "PW")):
        _expect_config_error(
            lambda f=field_name, v=value: parse_job_config(_excel_raw(**{f: v})),
            "doesn't apply",
        )


def test_excel_is_rejected_as_a_target_engine():
    raw = _valid_raw()
    raw["target"] = {"engine": "Excel/CSV", "database": "/data/out.xlsx"}
    _expect_config_error(lambda: parse_job_config(raw), "supported engines")


def test_resolve_connection_params_for_a_file_source_needs_no_env_var():
    config = parse_job_config(_excel_raw())
    params = resolve_connection_params(config.source)
    assert params.database == "/data/sales.xlsx"
    assert params.password == ""
    assert params.host == ""


def test_resolve_connection_params_for_a_file_source_keeps_the_schema():
    config = parse_job_config(_excel_raw(schema="reporting"))
    assert resolve_connection_params(config.source).schema == "reporting"


def test_excel_source_round_trips_through_a_real_json_file():
    base = _tmp_dir()
    try:
        path = base / "job.json"
        path.write_text(json.dumps(_excel_raw()), encoding="utf-8")
        config = load_job_config(path)
        assert config.source.engine == "Excel/CSV"
        assert config.source.database == "/data/sales.xlsx"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ------------------------------------------------ intra-table sharding options


def test_sharding_options_default_sensibly():
    config = parse_job_config(_valid_raw())
    # 0 means "use max_workers" -- there's no point splitting a table into
    # more pieces than there are workers to run them.
    assert config.max_shards_per_table == 0
    assert config.min_rows_to_shard > 0


def test_sharding_options_round_trip():
    config = parse_job_config(_valid_raw(max_workers=8, max_shards_per_table=16, min_rows_to_shard=250000))
    assert config.max_workers == 8
    assert config.max_shards_per_table == 16
    assert config.min_rows_to_shard == 250000


def test_max_shards_per_table_rejects_a_negative():
    _expect_config_error(
        lambda: parse_job_config(_valid_raw(max_shards_per_table=-1)), "cannot be negative")


def test_max_shards_per_table_rejects_a_non_number():
    _expect_config_error(
        lambda: parse_job_config(_valid_raw(max_shards_per_table="lots")), "must be a number")


def test_min_rows_to_shard_rejects_a_negative():
    _expect_config_error(
        lambda: parse_job_config(_valid_raw(min_rows_to_shard=-5)), "cannot be negative")


def test_min_rows_to_shard_rejects_a_non_number():
    _expect_config_error(
        lambda: parse_job_config(_valid_raw(min_rows_to_shard="many")), "must be a number")


def test_zero_shards_means_use_max_workers_not_an_error():
    config = parse_job_config(_valid_raw(max_shards_per_table=0))
    assert config.max_shards_per_table == 0


# ------------------------------------------------- two-phase DDL (defer_constraints)


def test_defer_constraints_defaults_off():
    # The single-script behavior is the long-standing default; deferral is
    # opt-in because it changes when a constraint violation surfaces.
    assert parse_job_config(_valid_raw()).defer_constraints is False


def test_defer_constraints_round_trips():
    assert parse_job_config(_valid_raw(defer_constraints=True)).defer_constraints is True


def test_defer_constraints_coerces_truthy_values():
    assert parse_job_config(_valid_raw(defer_constraints=1)).defer_constraints is True
    assert parse_job_config(_valid_raw(defer_constraints=0)).defer_constraints is False


def test_defer_constraints_is_a_known_field():
    # Guards against the unknown-field validator rejecting it.
    config = parse_job_config(_valid_raw(defer_constraints=True, apply_ddl=True, migrate=True))
    assert config.defer_constraints is True
    assert config.apply_ddl is True


# --------------------------------------------- checkpoint flush interval (1.5)


def test_checkpoint_flush_seconds_has_a_sensible_default():
    config = parse_job_config(_valid_raw())
    assert config.checkpoint_flush_seconds > 0


def test_checkpoint_flush_seconds_round_trips():
    assert parse_job_config(_valid_raw(checkpoint_flush_seconds=30)).checkpoint_flush_seconds == 30.0


def test_checkpoint_flush_seconds_accepts_zero_for_per_batch_writes():
    # 0 is the pre-throttling behavior -- the narrowest possible re-work
    # window on a crash, at the cost of a write after every batch.
    assert parse_job_config(_valid_raw(checkpoint_flush_seconds=0)).checkpoint_flush_seconds == 0.0


def test_checkpoint_flush_seconds_accepts_fractions():
    assert parse_job_config(_valid_raw(checkpoint_flush_seconds=0.5)).checkpoint_flush_seconds == 0.5


def test_checkpoint_flush_seconds_rejects_a_negative():
    _expect_config_error(
        lambda: parse_job_config(_valid_raw(checkpoint_flush_seconds=-1)), "cannot be negative")


def test_checkpoint_flush_seconds_rejects_a_non_number():
    _expect_config_error(
        lambda: parse_job_config(_valid_raw(checkpoint_flush_seconds="often")), "must be a number")


# ------------------------------------------------------- CDC section (2.1)


def _cdc_raw(**cdc_overrides):
    raw = _valid_raw()
    cdc = {"enabled": True}
    cdc.update(cdc_overrides)
    raw["cdc"] = cdc
    return raw


def test_cdc_is_absent_by_default():
    # A one-off copy shouldn't have to think about Kafka at all.
    config = parse_job_config(_valid_raw())
    assert config.cdc.enabled is False


def test_cdc_section_round_trips():
    config = parse_job_config(_cdc_raw(
        connect_url="http://kc:8083", kafka_bootstrap_servers="kafka:9092",
        connector_name="hr-cdc", topic_prefix="hrmig"))
    assert config.cdc.enabled is True
    assert config.cdc.connect_url == "http://kc:8083"
    assert config.cdc.connector_name == "hr-cdc"
    assert config.cdc.topic_prefix == "hrmig"


def test_cdc_defaults_to_no_data_snapshot_mode():
    # This tool does the bulk load; Debezium streams the delta.
    assert parse_job_config(_cdc_raw()).cdc.snapshot_mode == "no_data"


def test_cdc_rejects_an_unknown_snapshot_mode():
    _expect_config_error(
        lambda: parse_job_config(_cdc_raw(snapshot_mode="whenever")), "snapshot_mode")


def test_cdc_rejects_an_unknown_log_mining_strategy():
    _expect_config_error(
        lambda: parse_job_config(_cdc_raw(log_mining_strategy="guess")), "log_mining_strategy")


def test_cdc_rejects_unknown_fields():
    _expect_config_error(lambda: parse_job_config(_cdc_raw(kafka_topik="typo")), "unknown field")


def test_cdc_enabled_against_a_non_oracle_source_is_rejected():
    # Debezium's Oracle connector is the only capture path this tool
    # generates config for -- better to catch that here than at
    # connector-start time on the cluster.
    raw = _cdc_raw()
    raw["source"] = {
        "engine": "MySQL", "host": "h", "port": 3306, "database": "d",
        "username": "u", "password_env": "SRC_PW",
    }
    _expect_config_error(lambda: parse_job_config(raw), "Oracle connector")


def test_cdc_disabled_against_a_non_oracle_source_is_fine():
    raw = _valid_raw()
    raw["source"] = {
        "engine": "MySQL", "host": "h", "port": 3306, "database": "d",
        "username": "u", "password_env": "SRC_PW",
    }
    raw["cdc"] = {"enabled": False}
    assert parse_job_config(raw).cdc.enabled is False


def test_cdc_drain_thresholds_validate():
    config = parse_job_config(_cdc_raw(drain_target_seconds=30, drain_timeout_seconds=600))
    assert config.cdc.drain_target_seconds == 30.0
    assert config.cdc.drain_timeout_seconds == 600.0
    _expect_config_error(
        lambda: parse_job_config(_cdc_raw(drain_target_seconds=-1)), "cannot be negative")
    _expect_config_error(
        lambda: parse_job_config(_cdc_raw(drain_timeout_seconds="soon")), "must be a number")


def test_cdc_pdb_name_is_optional():
    assert parse_job_config(_cdc_raw()).cdc.database_pdb_name is None
    assert parse_job_config(_cdc_raw(database_pdb_name="ORCLPDB1")).cdc.database_pdb_name == "ORCLPDB1"


def test_cdc_must_be_an_object():
    raw = _valid_raw()
    raw["cdc"] = "yes please"
    _expect_config_error(lambda: parse_job_config(raw), '"cdc" must be an object')
