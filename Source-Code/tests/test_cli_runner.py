"""Tests for tgdatabridge.cli.runner -- headless CLI orchestration
(ENTERPRISE_READINESS.md section 5, items 1 and 3). Uses fake connectors
(matching tests/test_batch.py's _FakeConn pattern) instead of real
database drivers, and monkeypatches tgdatabridge.cli.runner.introspector_for
(restored in `finally`) so no real DB or GUI dependency is ever needed.
Plain functions, no pytest -- see tests/test_cli_config.py's docstring."""
import os
import pathlib
import shutil
import tempfile

import tgdatabridge.cli.runner as runner
from tgdatabridge.cli.config import parse_job_config
from tgdatabridge.core.schema_model import Column, Schema, Table
from tgdatabridge.core.validation import table_checksum

SRC_PW_VAR = "TGSCT_TEST_CLI_SRC_PW"
TGT_PW_VAR = "TGSCT_TEST_CLI_TGT_PW"


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="tgdatabridge_test_"))


def _job_raw(**overrides):
    raw = {
        "source": {"engine": "Oracle", "host": "srchost", "port": 1521, "database": "orcl",
                   "username": "hr_admin", "password_env": SRC_PW_VAR, "schema": "HR"},
        "target": {"engine": "PostgreSQL", "host": "tgthost", "port": 5432, "database": "hrdb",
                   "username": "postgres", "password_env": TGT_PW_VAR},
        "target_schema": "public",
    }
    raw.update(overrides)
    return raw


def _schema_with_one_table(rows=None):
    rows = rows if rows is not None else [(1,), (2,), (3,)]
    table = Table(name="EMPLOYEES", schema="HR", columns=[Column(name="ID", data_type="NUMBER")])
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL", tables=[table])
    return schema, rows


class _FakeConn:
    """A minimal stand-in DB connector: connects/closes, executes DDL,
    streams fixed rows, inserts batches, and reports counts/checksums that
    match between source and target so post-migration validation passes
    cleanly -- tests that want a validation *mismatch* build their own."""

    def __init__(self, rows=None, fail_ddl_on_statement=None):
        self.connected = False
        self.closed = False
        self.executed = []
        self.inserted = []
        self.schema_name = "HR"
        self.rows = rows if rows is not None else [(1,), (2,), (3,)]
        self.fail_ddl_on_statement = fail_ddl_on_statement

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True

    def execute_ddl(self, sql):
        self.executed.append(sql)
        if self.fail_ddl_on_statement is not None and len(self.executed) == self.fail_ddl_on_statement:
            raise RuntimeError("simulated DDL failure")

    def fetch_batches(self, sql, batch_size=5000):
        yield ["ID"], self.rows

    def insert_batch(self, table_name, columns, rows):
        self.inserted.append((table_name, list(rows)))

    def count_rows(self, table_name, schema=None):
        return len(self.rows)

    def checksum_rows(self, table_name, columns, schema=None, max_rows=50000):
        # Matches tgdatabridge.core.validation.table_checksum's own algorithm
        # (XOR of every row's row_checksum()) so that when a test's target
        # fake is given the *same* rows as its source fake, post-migration
        # validation genuinely passes -- not just a placeholder value that
        # happens to look equal on both sides.
        return table_checksum(self.rows)


def _patched_introspector(schema):
    return lambda engine: (lambda conn, schema_name: schema)


def _run(config, schema, rows=None, apply_ddl_fails_at=None, source_rows_for_target_check=None, **kwargs):
    """Runs config against fakes wired up with `schema`, returning
    (exit_code, printed_lines, source_conns, target_conns). run_job opens
    a fresh connection per step (Load Schema; Apply DDL; Migrate Data), so
    both lists may have more than one entry -- e.g. target_conns[0] is the
    Apply DDL connection, target_conns[-1] is the Migrate Data one."""
    lines = []
    src_conns = []
    tgt_conns = []

    def make_source(engine, params):
        c = _FakeConn(rows=rows)
        src_conns.append(c)
        return c

    def make_target(engine, params):
        c = _FakeConn(rows=source_rows_for_target_check if source_rows_for_target_check is not None else rows,
                       fail_ddl_on_statement=apply_ddl_fails_at)
        tgt_conns.append(c)
        return c

    original = runner.introspector_for
    runner.introspector_for = _patched_introspector(schema)
    try:
        code = runner.run_job(
            config, print_fn=lines.append,
            make_source_connector=make_source, make_target_connector=make_target,
            **kwargs,
        )
    finally:
        runner.introspector_for = original
    return code, lines, src_conns, tgt_conns


def _with_passwords(fn):
    os.environ[SRC_PW_VAR] = "srcpw"
    os.environ[TGT_PW_VAR] = "tgtpw"
    try:
        fn()
    finally:
        os.environ.pop(SRC_PW_VAR, None)
        os.environ.pop(TGT_PW_VAR, None)


# --------------------------------------------------------- check_approval_gate


def test_approval_gate_passes_when_not_production():
    config = parse_job_config(_job_raw(production=False))
    ok, reason = runner.check_approval_gate(config, approved_by=None)
    assert ok is True
    assert reason == ""


def test_approval_gate_blocks_production_without_approver():
    config = parse_job_config(_job_raw(production=True))
    ok, reason = runner.check_approval_gate(config, approved_by=None)
    assert ok is False
    assert "approver" in reason.lower()


def test_approval_gate_passes_production_with_approver_and_no_command():
    config = parse_job_config(_job_raw(production=True))
    ok, reason = runner.check_approval_gate(config, approved_by="Alice")
    assert ok is True


def test_approval_gate_runs_approval_command_and_honors_exit_code():
    config = parse_job_config(_job_raw(production=True, approval_command="exit 0"))
    ok, _ = runner.check_approval_gate(config, approved_by="Alice")
    assert ok is True

    config2 = parse_job_config(_job_raw(production=True, approval_command="exit 1"))
    ok2, reason2 = runner.check_approval_gate(config2, approved_by="Alice")
    assert ok2 is False
    assert "approval_command" in reason2 or "exited" in reason2.lower()


def test_approval_gate_blank_approver_is_treated_as_missing():
    config = parse_job_config(_job_raw(production=True))
    ok, _ = runner.check_approval_gate(config, approved_by="   ")
    assert ok is False


# --------------------------------------------------------------------- run_job


def test_run_job_assess_only_writes_files_and_returns_ok():
    def body():
        base = _tmp_dir()
        out = _tmp_dir()
        try:
            config = parse_job_config(_job_raw())  # apply_ddl=False, migrate=False by default
            schema, rows = _schema_with_one_table()
            code, lines, srcs, tgts = _run(config, schema, rows=rows, base_dir=base, output_dir=out)
            assert code == 0
            assert (out / "ddl.sql").exists()
            assert (out / "report.html").exists()
            assert not (out / "rollback.sql").exists()  # only written when apply_ddl/migrate actually run
            assert srcs[0].closed is True
            assert tgts == []  # neither apply_ddl nor migrate was requested

            from tgdatabridge.utils import app_storage
            history = app_storage.load_conversion_history(base_dir=base)
            assert len(history) == 1
            assert history[0].schema_name == "HR"
        finally:
            shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
    _with_passwords(body)


def test_run_job_apply_ddl_and_migrate_success():
    def body():
        base = _tmp_dir()
        out = _tmp_dir()
        try:
            config = parse_job_config(_job_raw(apply_ddl=True, migrate=True))
            schema, rows = _schema_with_one_table()
            code, lines, srcs, tgts = _run(config, schema, rows=rows, base_dir=base, output_dir=out)
            assert code == 0, lines
            assert (out / "rollback.sql").exists()
            assert len(tgts) == 2  # a fresh connection for Apply DDL, another for Migrate Data
            apply_conn, migrate_conn = tgts[0], tgts[-1]
            assert len(apply_conn.executed) > 0  # DDL statements were applied
            assert len(migrate_conn.inserted) == 1  # one table's worth of rows inserted

            from tgdatabridge.utils import metrics
            ops = {op["operation"] for op in metrics.load_operations(base_dir=base)}
            assert {"Load Schema", "Convert Schema", "Apply DDL to Target", "Migrate Data"} <= ops
        finally:
            shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
    _with_passwords(body)


def test_run_job_apply_ddl_failure_stops_before_migrate():
    def body():
        base = _tmp_dir()
        out = _tmp_dir()
        try:
            config = parse_job_config(_job_raw(apply_ddl=True, migrate=True))
            schema, rows = _schema_with_one_table()
            code, lines, srcs, tgts = _run(
                config, schema, rows=rows, base_dir=base, output_dir=out, apply_ddl_fails_at=1)
            assert code == 1
            assert len(tgts) == 1  # Apply DDL failed -- Migrate Data never opened a connection
            assert tgts[0].inserted == []  # migrate never ran
            assert any("ERROR" in line for line in lines)
        finally:
            shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
    _with_passwords(body)


def test_run_job_dry_run_migrate_writes_nothing_to_target():
    def body():
        base = _tmp_dir()
        out = _tmp_dir()
        try:
            config = parse_job_config(_job_raw(migrate=True, dry_run_migrate=True))
            schema, rows = _schema_with_one_table()
            code, lines, srcs, tgts = _run(config, schema, rows=rows, base_dir=base, output_dir=out)
            assert code == 0
            assert tgts[-1].inserted == []
            assert any("Dry run" in line for line in lines)
        finally:
            shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
    _with_passwords(body)


def test_run_job_cli_dry_run_flag_forces_plan_mode():
    def body():
        base = _tmp_dir()
        out = _tmp_dir()
        try:
            # migrate=True but dry_run_migrate not set in the file -- the
            # CLI's own --dry-run flag (main()) sets it before calling
            # run_job, simulated here by setting it directly, matching
            # what main() does.
            config = parse_job_config(_job_raw(migrate=True))
            config.dry_run_migrate = True
            schema, rows = _schema_with_one_table()
            code, lines, srcs, tgts = _run(config, schema, rows=rows, base_dir=base, output_dir=out)
            assert code == 0
            assert tgts[-1].inserted == []
        finally:
            shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
    _with_passwords(body)


def test_run_job_production_without_approver_blocks_before_touching_target():
    def body():
        base = _tmp_dir()
        out = _tmp_dir()
        try:
            config = parse_job_config(_job_raw(apply_ddl=True, migrate=True, production=True))
            schema, rows = _schema_with_one_table()
            code, lines, srcs, tgts = _run(config, schema, rows=rows, base_dir=base, output_dir=out, approved_by=None)
            assert code == 3
            assert any("BLOCKED" in line for line in lines)
            # ddl.sql/report.html are still written (the assess-only part
            # is always safe) but nothing touched the target.
            assert (out / "ddl.sql").exists()
            assert tgts == []
        finally:
            shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
    _with_passwords(body)


def test_run_job_production_with_approver_proceeds():
    def body():
        base = _tmp_dir()
        out = _tmp_dir()
        try:
            config = parse_job_config(_job_raw(apply_ddl=True, migrate=True, production=True))
            schema, rows = _schema_with_one_table()
            code, lines, srcs, tgts = _run(
                config, schema, rows=rows, base_dir=base, output_dir=out, approved_by="Alice")
            assert code == 0
            assert len(tgts[-1].inserted) == 1

            from tgdatabridge.utils import metrics
            convert_op = [op for op in metrics.load_operations(base_dir=base) if op["operation"] == "Convert Schema"][0]
            assert convert_op["approved_by"] == "Alice"
        finally:
            shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
    _with_passwords(body)


def test_run_job_table_filter_restricts_migration_scope():
    def body():
        base = _tmp_dir()
        out = _tmp_dir()
        try:
            t1 = Table(name="EMPLOYEES", schema="HR", columns=[Column(name="ID", data_type="NUMBER")])
            t2 = Table(name="DEPARTMENTS", schema="HR", columns=[Column(name="ID", data_type="NUMBER")])
            schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL", tables=[t1, t2])
            config = parse_job_config(_job_raw(migrate=True, tables=["employees"]))  # case-insensitive
            code, lines, srcs, tgts = _run(config, schema, rows=[(1,)], base_dir=base, output_dir=out)
            assert code == 0
            assert len(tgts[-1].inserted) == 1
            assert tgts[-1].inserted[0][0] == "EMPLOYEES"
        finally:
            shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
    _with_passwords(body)


def test_run_job_missing_password_env_raises_cli_config_error():
    from tgdatabridge.cli.config import CliConfigError
    base = _tmp_dir()
    out = _tmp_dir()
    os.environ.pop(SRC_PW_VAR, None)
    os.environ.pop(TGT_PW_VAR, None)
    try:
        config = parse_job_config(_job_raw())
        schema, rows = _schema_with_one_table()
        raised = False
        try:
            _run(config, schema, rows=rows, base_dir=base, output_dir=out)
        except CliConfigError:
            raised = True
        assert raised
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


# ------------------------------------------------------------------------ CLI


def test_build_arg_parser_requires_config():
    parser = runner.build_arg_parser()
    try:
        parser.parse_args([])
        raised = False
    except SystemExit:
        raised = True
    assert raised  # argparse exits when a required arg is missing


def test_main_returns_config_error_exit_code_for_bad_file():
    code = runner.main(["--config", "/definitely/does/not/exist.json"])
    assert code == 2


def test_main_returns_config_error_exit_code_for_malformed_json():
    base = _tmp_dir()
    try:
        path = base / "job.json"
        path.write_text("{not valid json", encoding="utf-8")
        code = runner.main(["--config", str(path), "--quiet"])
        assert code == 2
    finally:
        shutil.rmtree(base, ignore_errors=True)
