"""Headless CLI orchestration (ENTERPRISE_READINESS.md section 5, item 1:
"Headless/CLI mode") -- Load Schema -> Convert -> (optionally) Apply DDL /
Migrate Data driven by a config file (tgdatabridge.cli.config), for a script or
CI/CD pipeline. Built entirely on tgdatabridge.core.* / tgdatabridge.utils.* -- the same
modules tgdatabridge.gui.main_window's toolbar actions call into -- so a CLI run
gets the exact same conversion logic, reliability features (validation,
checkpoint/resume, retry-with-backoff), and observability (structured
logs, actor attribution, metrics) as a GUI run of the same steps.

Exit codes (also documented in --help):
    0   Every requested step completed with nothing failed.
    1   A step ran but failed (a connection/introspection error, a DDL
        statement failed to apply, or one or more tables failed to
        migrate / didn't validate).
    2   The config file or environment is wrong in some way that meant
        nothing ever touched a database (bad file, unknown engine, a
        password_env variable that isn't set, and so on).
    3   The job is marked production=true and the approval gate
        (item 3) blocked it -- no approver given, or approval_command
        exited non-zero.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from tgdatabridge.cli.config import (
    CliConfigError, CliJobConfig, load_job_config, resolve_ai_config, resolve_connection_params,
)
from tgdatabridge import version
from tgdatabridge.cdc import config as cdc_config
from tgdatabridge.cdc import orchestrator as cdc_orchestrator
from tgdatabridge.cdc.connect_client import ConnectError, KafkaConnectClient
from tgdatabridge.core import ddl_generator
from tgdatabridge.core.assessment import build_assessment
from tgdatabridge.core.connector_factory import (
    introspector_for, make_source_connector as _default_make_source_connector,
    make_target_connector as _default_make_target_connector,
)
from tgdatabridge.reports.report_generator import generate_html_report
from tgdatabridge.utils import app_storage, logger, metrics
from tgdatabridge.utils.sql_split import split_sql_statements

#: Names the approver for a production=true job. The TGSCT_ spelling is
#: the pre-rebrand one and is still honoured -- see main() -- so an
#: existing CI job keeps working across the rename.
APPROVER_ENV_VAR = "TGDATABRIDGE_APPROVED_BY"
LEGACY_APPROVER_ENV_VAR = "TGSCT_APPROVED_BY"

_EXIT_OK = 0
_EXIT_RUNTIME_FAILURE = 1
_EXIT_CONFIG_ERROR = 2
_EXIT_APPROVAL_BLOCKED = 3


def check_approval_gate(config: CliJobConfig, approved_by: Optional[str]) -> Tuple[bool, str]:
    """The "requires sign-off" gate from ENTERPRISE_READINESS.md section 5,
    item 3. Not gated at all when the job isn't marked production. When it
    is: always requires a human name (--approved-by / TGDATABRIDGE_APPROVED_BY),
    and additionally runs `config.approval_command` (if given) as the
    generic hook for "integrated with whatever change-management tool the
    org uses" -- any shell command that exits non-zero is treated as "not
    approved". Never raises; a broken approval_command is itself treated
    as "not approved" rather than crashing the run."""
    if not config.production:
        return True, ""
    if not approved_by or not approved_by.strip():
        return False, (
            "This job is marked production=true, which requires an approver. "
            f'Pass --approved-by "Name", or set the {APPROVER_ENV_VAR} environment variable.'
        )
    if config.approval_command:
        try:
            result = subprocess.run(config.approval_command, shell=True)
        except OSError as exc:
            return False, f"Could not run approval_command ({config.approval_command!r}): {exc}"
        if result.returncode != 0:
            return False, (
                f"approval_command exited with status {result.returncode} -- treating this as "
                f"\"not approved\" (approval_command: {config.approval_command!r})."
            )
    return True, ""


def _selected_tables(schema, table_names: Optional[List[str]]):
    if not table_names:
        return schema.tables
    wanted = {t.lower() for t in table_names}
    return [t for t in schema.tables if t.name.lower() in wanted]


def run_job(
    config: CliJobConfig,
    approved_by: Optional[str] = None,
    base_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    print_fn: Callable[[str], None] = print,
    make_source_connector: Callable = _default_make_source_connector,
    make_target_connector: Callable = _default_make_target_connector,
) -> int:
    """Runs one migration job end to end. `base_dir`, if given, overrides
    where conversion history / checkpoints / logs / metrics are read and
    written for this run (the same test/advanced-use seam every
    tgdatabridge.utils.app_storage function already accepts) -- handy for CI to
    pin a specific shared location explicitly (see --state-dir) rather
    than depending on a machine-local settings.json existing.
    `make_source_connector`/`make_target_connector` are dependency-injection
    seams for tests; real callers never need to pass them."""
    actor = logger.current_actor()
    out_dir = Path(output_dir if output_dir is not None else config.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    source_params = resolve_connection_params(config.source)
    target_params = resolve_connection_params(config.target)

    # ---- Load Schema ----------------------------------------------------
    start = time.monotonic()
    source = make_source_connector(config.source.engine, source_params)
    try:
        source.connect()
        schema_name = source.schema_name
        print_fn(f"Reading schema '{schema_name}' from {config.source.engine}...")
        introspect_schema = introspector_for(config.source.engine)
        schema = introspect_schema(source, schema_name)
    except Exception as exc:  # noqa: BLE001 - report and fail the run, don't crash the process
        _record_metrics("Load Schema", start, False, actor, base_dir)
        print_fn(f"ERROR: could not load the source schema: {exc}")
        logger.error(f"CLI: could not load the source schema: {exc}")
        _print_ai_explanation(config, str(exc), "Load Schema", print_fn)
        return _EXIT_RUNTIME_FAILURE
    finally:
        try:
            source.close()
        except Exception:  # noqa: BLE001 - closing a possibly-never-opened connection must never mask the real error
            pass

    schema.target_engine = config.target.engine
    print_fn(
        f"Loaded {len(schema.tables)} tables, {len(schema.views)} views, "
        f"{len(schema.sequences)} sequences, {len(schema.routines)} routines/triggers."
    )
    _record_metrics(
        "Load Schema", start, True, actor, base_dir,
        source_engine=config.source.engine, target_engine=config.target.engine)

    # ---- Convert Schema ---------------------------------------------------
    start = time.monotonic()
    # Two-phase DDL (SCALE.md section 1.3): with defer_constraints the
    # target's constraints, indexes and triggers are held back until the
    # data has landed, so the bulk load isn't maintaining an index and
    # re-checking a constraint on every row -- and triggers never fire on
    # migrated rows at all. `ddl_text` below is whichever script the
    # "Apply DDL" step should run *before* the data.
    post_load_ddl = ""
    if config.defer_constraints:
        ddl_text, post_load_ddl, _issues = ddl_generator.generate_schema_ddl_phased(
            schema, config.target.engine, config.target_schema)
    else:
        ddl_text, _issues = ddl_generator.generate_schema_ddl(
            schema, config.target.engine, config.target_schema)
    summary = build_assessment(schema)
    report_html = generate_html_report(schema, summary)

    if config.defer_constraints:
        (out_dir / "ddl_preload.sql").write_text(ddl_text, encoding="utf-8")
        (out_dir / "ddl_postload.sql").write_text(post_load_ddl, encoding="utf-8")
        written = "ddl_preload.sql, ddl_postload.sql and report.html"
    else:
        (out_dir / "ddl.sql").write_text(ddl_text, encoding="utf-8")
        written = "ddl.sql and report.html"
    (out_dir / "report.html").write_text(report_html, encoding="utf-8")
    print_fn(
        f"Converted: {summary.automatic_pct}% automatic, ~{summary.estimated_manual_hours}h estimated "
        f"manual effort, {len(summary.action_items)} action item(s). Wrote {written} to {out_dir}."
    )
    _record_metrics(
        "Convert Schema", start, True, actor, base_dir,
        source_engine=config.source.engine, target_engine=config.target.engine,
        total_objects=summary.total_objects, automatic_pct=summary.automatic_pct,
        approved_by=approved_by or "",
    )
    try:
        app_storage.record_conversion_run(
            schema_name=schema.name, source_engine=config.source.engine,
            source_database=source_params.database, target_engine=config.target.engine,
            target_database=target_params.database, target_schema=config.target_schema,
            total_objects=summary.total_objects, automatic_pct=summary.automatic_pct,
            estimated_manual_hours=summary.estimated_manual_hours,
            action_item_count=len(summary.action_items),
            report_html=report_html, ddl_text=ddl_text, actor=actor, base_dir=base_dir,
        )
    except OSError as exc:
        logger.warning(f"Could not record this run to conversion history: {exc}")

    if not config.apply_ddl and not config.migrate:
        return _EXIT_OK

    # ---- Approval gate (section 5, item 3) -------------------------------
    if config.production:
        approved, reason = check_approval_gate(config, approved_by)
        if not approved:
            print_fn(f"BLOCKED: {reason}")
            logger.error(f"CLI: production job blocked by approval gate: {reason}")
            return _EXIT_APPROVAL_BLOCKED
        logger.info(f"Production job approved by: {approved_by}")

    rollback_text = ddl_generator.generate_rollback_ddl(schema, config.target.engine, config.target_schema)
    (out_dir / "rollback.sql").write_text(rollback_text, encoding="utf-8")

    had_failure = False

    # ---- Apply DDL to Target ----------------------------------------------
    if config.apply_ddl:
        ok = _apply_ddl_script(
            ddl_text, "Apply DDL to Target", config, target_params,
            actor, base_dir, print_fn, make_target_connector,
        )
        had_failure = not ok

    if had_failure or not config.migrate:
        return _EXIT_RUNTIME_FAILURE if had_failure else _EXIT_OK

    # ---- Start change capture BEFORE the bulk load -------------------------
    # This ordering is load-bearing. With snapshot.mode=no_data the
    # connector records its start SCN on registration and buffers
    # everything after it; loading first leaves a gap that no row count
    # would ever reveal. See tgdatabridge/cdc/orchestrator.py.
    cdc_connector_config = None
    cdc_client = None
    if config.cdc.enabled and not config.dry_run_migrate and config.migrate:
        cdc_tables = _selected_tables(schema, config.tables)
        try:
            cdc_connector_config = _build_cdc_config(config, schema, source_params, cdc_tables)
            (out_dir / "debezium-connector.json").write_text(
                cdc_config.to_json(cdc_connector_config), encoding="utf-8")
            print_fn(f"Wrote debezium-connector.json to {out_dir}.")

            cdc_client = KafkaConnectClient(config.cdc.connect_url)
            cdc_source = make_source_connector(config.source.engine, source_params)
            try:
                cdc_source.connect()
                cdc_orchestrator.start_capture(
                    cdc_client, cdc_connector_config, source=cdc_source, print_fn=print_fn)
            finally:
                try:
                    cdc_source.close()
                except Exception:  # noqa: BLE001
                    pass
        except (cdc_config.CdcConfigError, cdc_orchestrator.CdcOrchestrationError, ConnectError) as exc:
            print_fn(f"ERROR: could not start change capture: {exc}")
            logger.error(f"CLI: could not start change capture: {exc}")
            # Deliberately fatal rather than "carry on without CDC": a
            # migration that silently proceeds with no capture running
            # produces a target that looks complete and is quietly stale
            # from the moment the load starts.
            return _EXIT_RUNTIME_FAILURE

    # ---- Migrate Data (or Dry Run / plan mode) -----------------------------
    tables = _selected_tables(schema, config.tables)
    label = "Dry Run (Plan)" if config.dry_run_migrate else "Migrate Data"
    start = time.monotonic()

    source = make_source_connector(config.source.engine, source_params)
    target = make_target_connector(config.target.engine, target_params)
    try:
        source.connect()
        target.connect()

        if config.dry_run_migrate:
            from tgdatabridge.core.migrator import plan_schema
            plan = plan_schema(source, target, tables)
            print_fn(
                f"Dry run: {plan.total_source_rows} source row(s) across {len(plan.tables)} table(s) "
                f"would be migrated. No data was written."
            )
            for t in plan.tables:
                for w in t.warnings:
                    print_fn(f"  {t.table_name}: {w}")
                if t.error:
                    print_fn(f"  {t.table_name}: {t.error}")
            _record_metrics(
                label, start, True, actor, base_dir,
                source_engine=config.source.engine, target_engine=config.target.engine,
                rows=plan.total_source_rows, not_ready=len(plan.not_ready),
            )
            if plan.not_ready:
                print_fn(f"Not ready: {', '.join(plan.not_ready)}")
                return _EXIT_RUNTIME_FAILURE
            return _EXIT_OK

        from tgdatabridge.core.migrator import migrate_schema
        from tgdatabridge.core.retry import RetryPolicy

        checkpoint_id = app_storage.checkpoint_id_for(
            source_params.database, target_params.database, schema.name, config.target.engine)
        checkpoint = app_storage.load_checkpoint(checkpoint_id, base_dir=base_dir) or app_storage.MigrationCheckpoint(
            checkpoint_id=checkpoint_id, schema_name=schema.name,
            source_engine=config.source.engine, target_engine=config.target.engine,
        )

        def on_retry(table_name, attempt, exc, delay):
            logger.warning(f"  {table_name}: transient error on attempt {attempt} ({exc}); retrying in {delay:.1f}s...")

        # Coalesces per-batch progress writes into at most one disk write
        # per interval; terminal transitions still write immediately via
        # .flush. See app_storage.CheckpointWriter and SCALE.md 1.5.
        checkpoint_writer = app_storage.CheckpointWriter(
            checkpoint, base_dir=base_dir, min_interval_seconds=config.checkpoint_flush_seconds)

        if config.max_workers > 1:
            # migrate_schema(max_workers>1) opens its own pool of
            # connections from these factories and refuses the single
            # pre-connected source/target above -- a bare DB-API connection
            # isn't safe to share across worker threads. Before this, a
            # config with max_workers > 1 raised ValueError and the option
            # was effectively unusable from the CLI.
            def source_factory():
                conn = make_source_connector(config.source.engine, source_params)
                conn.connect()
                return conn

            def target_factory():
                conn = make_target_connector(config.target.engine, target_params)
                conn.connect()
                return conn

            def on_shard_plan(table_name, shard_count, reason):
                if shard_count > 1:
                    print_fn(f"  {table_name}: split into {shard_count} parallel shards ({reason}).")

            report = migrate_schema(
                None, None, tables, retry_policy=RetryPolicy(), on_retry=on_retry,
                checkpoint=checkpoint, on_checkpoint_update=checkpoint_writer.request,
                on_checkpoint_flush=checkpoint_writer.flush,
                max_workers=config.max_workers,
                source_factory=source_factory, target_factory=target_factory,
                max_shards_per_table=config.max_shards_per_table or config.max_workers,
                min_rows_to_shard=config.min_rows_to_shard,
                on_shard_plan=on_shard_plan,
            )
        else:
            report = migrate_schema(
                source, target, tables, retry_policy=RetryPolicy(), on_retry=on_retry,
                checkpoint=checkpoint, on_checkpoint_update=checkpoint_writer.request,
                on_checkpoint_flush=checkpoint_writer.flush,
            )
    except Exception as exc:  # noqa: BLE001
        _record_metrics(label, start, False, actor, base_dir)
        print_fn(f"ERROR: {exc}")
        logger.error(f"CLI: Migrate Data failed: {exc}")
        _print_ai_explanation(config, str(exc), label, print_fn)
        return _EXIT_RUNTIME_FAILURE
    finally:
        for conn in (source, target):
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    failed = report.failed_tables
    unvalidated = report.unvalidated_tables
    print_fn(f"Migrated {report.total_rows} row(s) across {len(tables)} table(s).")
    for r in report.results:
        if not r.succeeded:
            print_fn(f"  FAILED {r.table_name}: {r.error}")
    if unvalidated:
        print_fn(f"Unvalidated tables: {', '.join(unvalidated)}")
    _record_metrics(
        label, start, True, actor, base_dir,
        source_engine=config.source.engine, target_engine=config.target.engine,
        rows=report.total_rows, failed_tables=len(failed), table_count=len(tables),
    )

    if not failed:
        app_storage.delete_checkpoint(checkpoint_id, base_dir=base_dir)

    # ---- Apply post-load DDL (constraints, indexes, triggers) --------------
    # Only after the data is actually in place, and only if it got there:
    # building a primary key over a half-migrated table would either fail
    # or, worse, succeed and leave a schema that looks finished but isn't.
    if config.defer_constraints and config.apply_ddl and post_load_ddl:
        if failed:
            print_fn(
                "Skipping post-load DDL (constraints, indexes, triggers): one or more tables "
                "failed to migrate. Fix those, re-run to resume, and the post-load step will "
                f"run then. The script is saved at {out_dir / 'ddl_postload.sql'} if you'd "
                "rather apply it by hand."
            )
        else:
            ok = _apply_ddl_script(
                post_load_ddl, "Apply Post-Load DDL", config, target_params,
                actor, base_dir, print_fn, make_target_connector,
            )
            if not ok:
                # The data is all there; only the constraints/indexes are
                # missing. Say so, because the remedy is very different
                # from a failed migration -- and a constraint that fails
                # here is usually telling you something real about the
                # data, not about the tool.
                print_fn(
                    "The data migrated successfully but the post-load constraints/indexes did not "
                    "all apply. A constraint failure here normally means the source data violates "
                    f"it. Review {out_dir / 'ddl_postload.sql'} and apply it by hand once resolved."
                )
                return _EXIT_RUNTIME_FAILURE

    # ---- Drain change capture ---------------------------------------------
    if cdc_client is not None and cdc_connector_config is not None:
        if failed:
            print_fn(
                "Skipping the CDC drain: one or more tables failed to migrate. The connector is "
                "still running and buffering, so fix those tables and re-run -- nothing has been "
                "lost.")
        else:
            drain_source = make_source_connector(config.source.engine, source_params)
            try:
                drain_source.connect()
                print_fn(
                    "Bulk load complete. Waiting for change capture to catch up before this "
                    "migration is cutover-ready...")
                cdc_orchestrator.wait_for_drain(
                    drain_source, cdc_client, cdc_connector_config.connector_name,
                    target_seconds=config.cdc.drain_target_seconds,
                    timeout=config.cdc.drain_timeout_seconds,
                    print_fn=print_fn,
                )
                print_fn(
                    "CDC lag is within the cutover threshold. Stop writes to the source, let the "
                    "last delta apply, validate, then switch over.")
            except (cdc_orchestrator.CdcOrchestrationError, ConnectError) as exc:
                print_fn(f"ERROR: change capture did not drain: {exc}")
                logger.error(f"CLI: change capture did not drain: {exc}")
                return _EXIT_RUNTIME_FAILURE
            finally:
                try:
                    drain_source.close()
                except Exception:  # noqa: BLE001
                    pass

    return _EXIT_RUNTIME_FAILURE if (failed or unvalidated) else _EXIT_OK


def run_cdc_preflight(
    config: CliJobConfig,
    base_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    print_fn: Callable[[str], None] = print,
    make_source_connector: Callable = _default_make_source_connector,
) -> int:
    """Load the schema, check the Oracle CDC prerequisites, and write the
    connector config -- without touching the target or migrating anything.

    Separate from run_job on purpose: the point of a preflight is to be
    run days ahead, by someone who is *not* mid-cutover, so it must be
    impossible for it to change anything.
    """
    out_dir = Path(output_dir if output_dir is not None else config.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source_params = resolve_connection_params(config.source)

    source = make_source_connector(config.source.engine, source_params)
    try:
        source.connect()
        schema_name = source.schema_name
        print_fn(f"Reading schema '{schema_name}' from {config.source.engine}...")
        schema = introspector_for(config.source.engine)(source, schema_name)
        tables = _selected_tables(schema, config.tables)

        try:
            connector_config = _build_cdc_config(config, schema, source_params, tables)
        except cdc_config.CdcConfigError as exc:
            print_fn(f"ERROR: {exc}")
            return _EXIT_CONFIG_ERROR

        (out_dir / "debezium-connector.json").write_text(
            cdc_config.to_json(connector_config), encoding="utf-8")
        print_fn(f"Wrote debezium-connector.json to {out_dir}.")
        for note in cdc_config.ordering_notes(connector_config):
            print_fn(f"NOTE: {note}")

        print_fn("")
        print_fn(f"CDC preflight for {schema_name} ({len(connector_config.table_names)} table(s)):")
        from tgdatabridge.cdc import preflight as cdc_preflight
        report = cdc_preflight.run_preflight(
            source, connector_config.schema_name, connector_config.table_names,
            connector_config.database_user)
        print_fn(report.render())
    except Exception as exc:  # noqa: BLE001
        print_fn(f"ERROR: CDC preflight could not run: {exc}")
        logger.error(f"CLI: CDC preflight could not run: {exc}")
        return _EXIT_RUNTIME_FAILURE
    finally:
        try:
            source.close()
        except Exception:  # noqa: BLE001
            pass

    print_fn("")
    if report.ready:
        extra = ""
        if report.warnings or report.unknowns:
            extra = (f" ({len(report.warnings)} warning(s), {len(report.unknowns)} unverifiable) "
                      "-- review those before relying on this.")
        print_fn(f"CDC preflight PASSED.{extra}")
        return _EXIT_OK
    print_fn(
        f"CDC preflight FAILED: {len(report.failures)} blocking problem(s). The remedial SQL is "
        "above. Do not start a cutover until these are resolved -- a connector started without "
        "them produces change events with missing before-images, which corrupt the target "
        "silently.")
    return _EXIT_RUNTIME_FAILURE


def _build_cdc_config(config: CliJobConfig, schema, source_params, tables):
    """Derive the Debezium connector config from the schema just loaded,
    so the capture include-list can't drift from the migration set."""
    return cdc_config.config_from_schema(
        schema,
        connector_name=config.cdc.connector_name,
        database_hostname=source_params.host,
        database_port=source_params.port,
        database_user=source_params.username,
        database_dbname=source_params.database,
        topic_prefix=config.cdc.topic_prefix,
        kafka_bootstrap_servers=config.cdc.kafka_bootstrap_servers,
        table_names=[t.name for t in tables],
        snapshot_mode=config.cdc.snapshot_mode,
        log_mining_strategy=config.cdc.log_mining_strategy,
        database_pdb_name=config.cdc.database_pdb_name,
        password_env=config.source.password_env,
    )


def _apply_ddl_script(
    ddl_text, label, config, target_params, actor, base_dir, print_fn, make_target_connector,
) -> bool:
    """Run one DDL script against the target, statement by statement.
    Used for both the pre-load script and (with defer_constraints) the
    post-load constraints/indexes/triggers script -- identical mechanics,
    only the label and the text differ. Returns True on success."""
    from tgdatabridge.core.fk_recovery import POLICY_NOT_VALID, recover, summarise
    from tgdatabridge.core.fk_violations import is_foreign_key_violation
    from tgdatabridge.utils.ddl_errors import describe_skip, is_already_exists_error

    start = time.monotonic()
    target = make_target_connector(config.target.engine, target_params)
    # Whatever the GUI would do with a re-run or with orphaned rows, an
    # unattended run has to do too: a scheduled migration that stops dead on
    # "this constraint already exists" or on one bad row in a million is a
    # migration nobody can automate. Same two helpers, same defaults.
    policy = getattr(getattr(config, "target", None), "fk_policy", None) or POLICY_NOT_VALID
    try:
        target.connect()
        statements = split_sql_statements(ddl_text)
        recoveries = []
        for idx, stmt in enumerate(statements, start=1):
            try:
                target.execute_ddl(stmt + ";")
            except Exception as exc:  # noqa: BLE001 - surface which statement failed, matching the GUI's own wrapping
                if is_already_exists_error(exc, stmt):
                    print_fn(f"{label}: statement {idx}/{len(statements)} skipped -- "
                             f"{describe_skip(stmt)} already exists on the target.")
                    logger.warning(f"CLI: {label}: {describe_skip(stmt)} already exists.")
                    continue
                if is_foreign_key_violation(exc):
                    done = recover(target, stmt, policy)
                    if done is not None and done.ok:
                        recoveries.append(done)
                        print_fn(f"{label}: statement {idx}/{len(statements)} -- {done.one_line()}")
                        logger.warning(f"CLI: {label}: {done.one_line()}")
                        continue
                preview = " ".join(stmt.split())[:120]
                raise RuntimeError(f"Statement {idx}/{len(statements)} failed: {exc}\n  -> {preview}...") from exc
        print_fn(f"{label}: applied {len(statements)} DDL statement(s) to {config.target.engine} target.")
        if recoveries:
            print_fn(summarise(recoveries))
        _record_metrics(
            label, start, True, actor, base_dir,
            source_engine=config.source.engine, target_engine=config.target.engine,
            statements=len(statements),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        _record_metrics(label, start, False, actor, base_dir)
        print_fn(f"ERROR: {exc}")
        logger.error(f"CLI: {label} failed: {exc}")
        _print_ai_explanation(config, str(exc), label, print_fn)
        return False
    finally:
        try:
            target.close()
        except Exception:  # noqa: BLE001
            pass


def _record_metrics(operation: str, start_time: float, success: bool, actor: str,
                     base_dir: Optional[Path], **extra) -> None:
    # Metrics are purely observational -- see main_window.py's identical
    # _record_operation_metrics rationale.
    try:
        duration = time.monotonic() - start_time
        metrics.record_operation(operation, duration, success, actor=actor, base_dir=base_dir, **extra)
    except Exception:  # noqa: BLE001
        pass


def _print_ai_explanation(config: CliJobConfig, error_text: str, context: str,
                           print_fn: Callable[[str], None]) -> None:
    """Best-effort: if this job's "ai" block turns on error diagnosis
    (tgdatabridge/ai/error_diagnostics.py), print a plain-language
    explanation and fix suggestions right after the ERROR line each
    failure branch in run_job already prints. Entirely additive and
    entirely optional -- resolve_ai_config returns None for the ordinary
    case (no "ai" block, or ai.enabled left False) and this is a no-op;
    any problem reaching the AI service is printed as one more line, not
    raised, since a step that already failed must still return its real
    exit code regardless of whether an explanation could be fetched."""
    try:
        ai_config = resolve_ai_config(config)
    except CliConfigError as exc:
        print_fn(f"(AI explanation skipped: {exc})")
        return
    if ai_config is None or not ai_config.feature_error_diagnostics:
        return

    from tgdatabridge.ai.ai_client import AiClient, AiError
    from tgdatabridge.ai.error_diagnostics import explain_error
    try:
        diagnosis = explain_error(AiClient(ai_config), error_text, context=context)
    except AiError as exc:
        print_fn(f"(AI explanation unavailable: {exc})")
        return

    if diagnosis.explanation:
        print_fn(f"AI explanation: {diagnosis.explanation}")
    for i, fix in enumerate(diagnosis.suggested_fixes, start=1):
        print_fn(f"  suggested fix {i}: {fix}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_cli.py",
        description=f"Headless/CI-friendly runner for {version.PRODUCT} -- {version.TAGLINE} "
                     f"(build {version.BUILD}). Drives Load Schema -> Convert -> "
                     "(optionally) Apply DDL / Migrate Data from a config file.",
        epilog="Exit codes: 0 success, 1 a step ran but failed, 2 config/environment error "
               "(nothing touched a database), 3 blocked by the production approval gate.",
    )
    parser.add_argument("-c", "--config", required=True, help="Path to a JSON or YAML migration config file.")
    parser.add_argument("--approved-by", default=None,
                         help="Approver name for a production=true job. Falls back to the "
                              f"{APPROVER_ENV_VAR} environment variable.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Force plan-only mode for the migrate step, regardless of the config file "
                              "(no data is written to the target).")
    parser.add_argument("--state-dir", default=None,
                         help="Override where conversion history/checkpoints/logs/metrics are read and "
                              "written for this run, instead of this machine's usual local (or configured "
                              "shared) storage location.")
    parser.add_argument("--output-dir", default=None,
                         help="Override the config file's output_dir for ddl.sql/report.html/rollback.sql.")
    parser.add_argument("--quiet", action="store_true", help="Don't echo log lines to stdout as they happen.")
    parser.add_argument("--cdc-preflight", action="store_true",
                         help="Check the Oracle source's CDC prerequisites (archivelog mode, "
                              "supplemental logging, LogMiner grants, redo retention) and write "
                              "debezium-connector.json, then exit without touching either "
                              "database. Run this days before a cutover, not on the night.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.quiet:
        logger.subscribe(lambda level, line: print(line, file=sys.stderr if level == "error" else sys.stdout))

    # Central log shipping used to be bootstrapped here, POSTing every log
    # line to a configured collector. It was removed: logs are written to
    # the local machine only (see tgdatabridge.utils.logger.log_dir). A CI runner
    # that wants them centrally should collect the .jsonl file as a build
    # artifact, which is the same mechanism it already uses for everything
    # else the run produces.

    try:
        config = load_job_config(Path(args.config))
    except CliConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    if args.dry_run:
        config.dry_run_migrate = True

    # TGSCT_APPROVED_BY is the pre-rebrand spelling. It is still read, and
    # deliberately not deprecated with a warning: this variable lives in
    # somebody's CI job definition or scheduled task, not in this repo, and
    # a rename that silently turns an approved production job into a
    # refused one is exactly the kind of breakage a rebrand must not cause.
    approved_by = (args.approved_by
                   or os.environ.get(APPROVER_ENV_VAR)
                   or os.environ.get(LEGACY_APPROVER_ENV_VAR))
    output_dir = Path(args.output_dir) if args.output_dir else None
    base_dir = Path(args.state_dir) if args.state_dir else None

    try:
        if args.cdc_preflight:
            return run_cdc_preflight(config, base_dir=base_dir, output_dir=output_dir)
        return run_job(config, approved_by=approved_by, base_dir=base_dir, output_dir=output_dir)
    except CliConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
