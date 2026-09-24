"""Multi-schema / whole-database batch migration orchestration (see
ENTERPRISE_READINESS.md section 4, "Scale & performance", item 3).

tgdatabridge.core.migrator.migrate_schema already migrates every table of *one*
schema; this module is the thin layer above it that runs that same
function once per schema across a whole batch, aggregating the results.
It is deliberately narrow in scope, matching this codebase's established
pattern for section 6's grammar-parser work: this is a core, GUI-
independent orchestration API (tgdatabridge/core/* has always been usable
without the GUI -- see ENTERPRISE_READINESS.md section 5), not a GUI
feature. The desktop GUI's "Migrate Data" flow still operates on exactly
one already-loaded `self.schema` at a time; wiring a multi-schema picker
into main_window.py (enumerating every schema/database on a source
connection, letting the user select several, running Load -> Convert ->
Apply DDL -> Migrate Data for each) is real, additional GUI work still
open -- this module is what such a feature (or a future headless/CLI
mode, see section 5 item 1) would call into.

Schemas are always migrated one at a time, never concurrently with each
other -- only the tables *within* one schema are ever parallelized (via
each job's own `max_workers`, exactly as migrate_schema already
supports). This is a deliberate, conservative default: unlike two tables
in the same FK-dependency wave (which this tool already knows have no
relationship to each other), two different schemas could easily share a
target database instance, a source database instance, or both, and this
module has no schema-level dependency information at all (no equivalent
of order_tables_by_dependency for cross-schema FKs, which Oracle does
allow) -- so a batch run gives each schema the full, serial run that a
human clicking through the GUI once per schema would, just without the
clicking. Running schema jobs concurrently too could be added later if a
concrete need for it shows up (e.g. schemas known to be fully
independent), but isn't attempted here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from tgdatabridge.core.migrator import MigrationReport, migrate_schema
from tgdatabridge.core.retry import RetryPolicy
from tgdatabridge.core.schema_model import Table


@dataclass
class SchemaMigrationJob:
    """One schema's worth of work for run_batch_migration -- everything
    migrate_schema itself needs, plus the schema's own name for reporting.

    `source_factory`/`target_factory` are always required here (unlike
    migrate_schema, which only requires them when max_workers > 1) --
    run_batch_migration uses them to open and close this job's own
    connection(s) itself (see its own docstring), so every job's
    connection lifecycle is handled uniformly regardless of that job's
    own max_workers."""
    schema_name: str
    tables: List[Table]
    source_factory: Callable[[], object]
    target_factory: Callable[[], object]
    max_workers: int = 1
    batch_size: int = 5000
    lob_batch_size: Optional[int] = 2000
    validate: bool = True
    checksum_max_rows: int = 50000
    retry_policy: Optional[RetryPolicy] = None
    checkpoint: Optional[object] = None  # app_storage.MigrationCheckpoint | None -- one per job, since checkpoints are already keyed by (source db, target db, schema, target engine)
    progress_cb: Optional[Callable[[str, int], None]] = None
    table_progress_cb: Optional[Callable[[int, int], None]] = None
    on_retry: Optional[Callable[[str, int, Exception, float], None]] = None
    on_checkpoint_update: Optional[Callable[[], None]] = None


@dataclass
class BatchReport:
    schema_reports: Dict[str, MigrationReport] = field(default_factory=dict)
    # A job that failed before migrate_schema ever got a chance to
    # produce a per-table MigrationReport at all -- e.g. its
    # source_factory()/target_factory() itself raised (bad credentials,
    # unreachable host, ...) -- goes here instead, keyed by schema_name,
    # since there's no MigrationReport to attach the error to.
    schema_errors: Dict[str, str] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(r.total_rows for r in self.schema_reports.values())

    @property
    def failed_schemas(self) -> List[str]:
        """Every schema that either failed outright (schema_errors) or
        completed with at least one failed table (a MigrationReport whose
        own failed_tables is non-empty) -- sorted for deterministic
        output, since dict iteration order isn't guaranteed to match job
        submission order across every Python version this tool supports."""
        names = set(self.schema_errors)
        for name, report in self.schema_reports.items():
            if report.failed_tables:
                names.add(name)
        return sorted(names)


def run_batch_migration(
    jobs: List[SchemaMigrationJob],
    schema_progress_cb: Optional[Callable[[int, int], None]] = None,
) -> BatchReport:
    """Runs migrate_schema once per job, in the order given -- see this
    module's own docstring for why schemas themselves are never run
    concurrently with each other (each job's own `max_workers` still
    parallelizes that schema's own tables, exactly as migrate_schema
    always has).

    `schema_progress_cb(schemas_done, total_schemas)` reports batch-level
    progress once each schema finishes -- the whole-batch counterpart of
    migrate_schema's own per-table `table_progress_cb`. A job whose
    connector factories raise (or whose migrate_schema call itself raises
    for some other reason outside the ordinary per-table error handling
    migrate_table already does) is recorded in the returned BatchReport's
    `schema_errors` and the batch continues on to the next job, rather
    than one bad schema aborting the whole run."""
    report = BatchReport()
    total = len(jobs)
    for done, job in enumerate(jobs, start=1):
        try:
            if job.max_workers > 1:
                schema_report = migrate_schema(
                    None, None, job.tables,
                    batch_size=job.batch_size,
                    progress_cb=job.progress_cb,
                    table_progress_cb=job.table_progress_cb,
                    retry_policy=job.retry_policy,
                    on_retry=job.on_retry,
                    checkpoint=job.checkpoint,
                    on_checkpoint_update=job.on_checkpoint_update,
                    validate=job.validate,
                    checksum_max_rows=job.checksum_max_rows,
                    lob_batch_size=job.lob_batch_size,
                    max_workers=job.max_workers,
                    source_factory=job.source_factory,
                    target_factory=job.target_factory,
                )
            else:
                source = job.source_factory()
                target = job.target_factory()
                try:
                    schema_report = migrate_schema(
                        source, target, job.tables,
                        batch_size=job.batch_size,
                        progress_cb=job.progress_cb,
                        table_progress_cb=job.table_progress_cb,
                        retry_policy=job.retry_policy,
                        on_retry=job.on_retry,
                        checkpoint=job.checkpoint,
                        on_checkpoint_update=job.on_checkpoint_update,
                        validate=job.validate,
                        checksum_max_rows=job.checksum_max_rows,
                        lob_batch_size=job.lob_batch_size,
                    )
                finally:
                    source.close()
                    target.close()
            report.schema_reports[job.schema_name] = schema_report
        except Exception as exc:  # noqa: BLE001 - one bad schema must not abort the whole batch
            report.schema_errors[job.schema_name] = str(exc)
        if schema_progress_cb:
            schema_progress_cb(done, total)
    return report
