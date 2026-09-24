"""Oracle-side prerequisite checks for Debezium CDC -- SCALE.md section 2.1.

Every check here exists because skipping it produces a failure that is
either delayed, misleading, or both:

  - **ARCHIVELOG mode off** -- the connector registers fine and then
    can't mine anything. The error names LogMiner, not archiving.
  - **No minimal supplemental logging** -- the connector runs and emits
    events whose `before` images are empty. Updates and deletes arrive
    with nothing to key on. Nothing errors; the data is just quietly
    wrong at the sink.
  - **No per-table supplemental logging (ALL) COLUMNS** -- same shape of
    problem, but only for the tables that are missing it, so it looks
    like an application bug rather than a configuration one.
  - **Missing LogMiner grants** -- fails at connector start with an
    ORA-00942 naming an internal view most people have never heard of.
  - **Short redo retention** -- everything works until the bulk load
    takes longer than the retention window, at which point the connector
    needs a log that has already been deleted and cannot resume. This is
    the one that bites specifically because the load is slow, which is
    to say: exactly on a 1 TB migration.

Running these up front, against the real source, turns all of that into
one report with the remedial SQL next to each gap. Every check degrades
to "unknown" rather than raising if the account can't read the relevant
view -- a preflight tool that itself crashes on a permissions problem is
not much use.

The SQL here is read-only. Nothing in this module changes the source
database: the fixes are *printed*, not applied. Enabling supplemental
logging on a production Oracle instance is a DBA's decision with a real
performance cost, and this tool has no business making it silently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_UNKNOWN = "unknown"

# Below this, a long bulk load can outrun the archive log retention and
# leave the connector unable to resume. Oracle's own default is 0
# (retain until backed up), which is why this is a warning rather than a
# hard failure -- 0 can be perfectly fine or completely wrong depending
# on the backup regime, and this tool can't tell which.
MIN_RETENTION_MINUTES = 24 * 60


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    remedy: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_PASS, STATUS_WARN, STATUS_UNKNOWN)


@dataclass
class PreflightReport:
    results: List[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == STATUS_FAIL]

    @property
    def warnings(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == STATUS_WARN]

    @property
    def unknowns(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == STATUS_UNKNOWN]

    @property
    def ready(self) -> bool:
        """True when nothing outright failed. Warnings and unknowns don't
        block -- they're judgement calls (retention policy) or things this
        account simply couldn't see, and refusing to proceed on either
        would make the check an obstacle rather than a safeguard."""
        return not self.failures

    def render(self) -> str:
        lines = []
        symbols = {STATUS_PASS: "PASS", STATUS_FAIL: "FAIL", STATUS_WARN: "WARN", STATUS_UNKNOWN: "????"}
        for result in self.results:
            lines.append(f"[{symbols.get(result.status, '????')}] {result.name}: {result.detail}")
            if result.remedy:
                for remedy_line in result.remedy.strip().splitlines():
                    lines.append(f"         {remedy_line}")
        return "\n".join(lines)


def _scalar(source, sql: str):
    """First column of the first row, or None if the query can't run.
    A preflight check that raises on a permissions problem is worse than
    one that reports 'couldn't tell'."""
    try:
        rows = list(source.execute(sql))
    except Exception:  # noqa: BLE001 - see this module's docstring
        return None
    if not rows or not rows[0]:
        return None
    return rows[0][0]


def check_archivelog_mode(source) -> CheckResult:
    value = _scalar(source, "SELECT LOG_MODE FROM V$DATABASE")
    if value is None:
        return CheckResult(
            "ARCHIVELOG mode", STATUS_UNKNOWN,
            "could not read V$DATABASE (the connecting account may lack SELECT on it)",
            "GRANT SELECT ON V_$DATABASE TO <cdc_user>;")
    if str(value).upper() == "ARCHIVELOG":
        return CheckResult("ARCHIVELOG mode", STATUS_PASS, "enabled")
    return CheckResult(
        "ARCHIVELOG mode", STATUS_FAIL,
        f"database is in {value} mode; LogMiner has no archived redo to read",
        "SHUTDOWN IMMEDIATE;\nSTARTUP MOUNT;\nALTER DATABASE ARCHIVELOG;\nALTER DATABASE OPEN;")


def check_minimal_supplemental_logging(source) -> CheckResult:
    value = _scalar(source, "SELECT SUPPLEMENTAL_LOG_DATA_MIN FROM V$DATABASE")
    if value is None:
        return CheckResult(
            "Minimal supplemental logging", STATUS_UNKNOWN,
            "could not read V$DATABASE",
            "GRANT SELECT ON V_$DATABASE TO <cdc_user>;")
    # Oracle reports YES or IMPLICIT when it's on; NO when it isn't.
    if str(value).upper() in ("YES", "IMPLICIT"):
        return CheckResult("Minimal supplemental logging", STATUS_PASS, f"enabled ({value})")
    return CheckResult(
        "Minimal supplemental logging", STATUS_FAIL,
        "disabled -- change events would carry empty 'before' images, so updates and deletes "
        "arrive at the sink with nothing to key on. Nothing errors; the data is just wrong.",
        "ALTER DATABASE ADD SUPPLEMENTAL LOG DATA;")


def check_table_supplemental_logging(source, schema_name: str, table_names: List[str]) -> CheckResult:
    """Per-table ALL COLUMNS supplemental logging.

    Debezium needs the full pre-image per captured table; minimal
    database-level logging alone isn't enough. A table missing this
    produces partial events for that table only, which reads like an
    application bug rather than a configuration one -- hence checking
    every captured table by name rather than spot-checking.
    """
    if not table_names:
        return CheckResult("Per-table supplemental logging", STATUS_UNKNOWN, "no tables to check")

    quoted = ", ".join(f"'{name.upper()}'" for name in table_names)
    sql = (
        "SELECT TABLE_NAME FROM ALL_LOG_GROUPS "
        f"WHERE OWNER = '{schema_name.upper()}' "
        f"AND TABLE_NAME IN ({quoted}) "
        "AND LOG_GROUP_TYPE = 'ALL COLUMN LOGGING'"
    )
    try:
        rows = list(source.execute(sql))
    except Exception:  # noqa: BLE001
        return CheckResult(
            "Per-table supplemental logging", STATUS_UNKNOWN,
            "could not read ALL_LOG_GROUPS",
            "GRANT SELECT ON ALL_LOG_GROUPS TO <cdc_user>;")

    logged = {str(r[0]).upper() for r in rows if r}
    missing = sorted({n.upper() for n in table_names} - logged)
    if not missing:
        return CheckResult(
            "Per-table supplemental logging", STATUS_PASS,
            f"all {len(table_names)} captured table(s) have ALL COLUMN logging")

    shown = ", ".join(missing[:10]) + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else "")
    remedy = "\n".join(
        f"ALTER TABLE {schema_name.upper()}.{name} ADD SUPPLEMENTAL LOG DATA (ALL) COLUMNS;"
        for name in missing[:10]
    )
    if len(missing) > 10:
        remedy += f"\n-- ... and {len(missing) - 10} more; see the full list in the report."
    return CheckResult(
        "Per-table supplemental logging", STATUS_FAIL,
        f"{len(missing)} of {len(table_names)} captured table(s) missing ALL COLUMN logging: {shown}",
        remedy)


# The views the Oracle connector reads while mining. Missing SELECT on any
# of them fails at connector start with an ORA-00942 naming a view most
# people have never heard of, which is a miserable thing to debug at 2am.
_REQUIRED_VIEWS = (
    "V_$DATABASE", "V_$LOG", "V_$LOGFILE", "V_$ARCHIVED_LOG",
    "V_$LOGMNR_CONTENTS", "V_$LOGMNR_LOGS", "V_$ARCHIVE_DEST_STATUS",
)


def check_logminer_privileges(source, username: str) -> CheckResult:
    quoted = ", ".join(f"'{v}'" for v in _REQUIRED_VIEWS)
    sql = (
        "SELECT TABLE_NAME FROM ALL_TAB_PRIVS "
        f"WHERE GRANTEE = '{username.upper()}' AND PRIVILEGE = 'SELECT' "
        f"AND TABLE_NAME IN ({quoted})"
    )
    try:
        rows = list(source.execute(sql))
    except Exception:  # noqa: BLE001
        return CheckResult(
            "LogMiner privileges", STATUS_UNKNOWN,
            "could not read ALL_TAB_PRIVS -- grants could not be verified",
            "Verify by hand that the CDC account can SELECT the V_$LOGMNR_* views.")

    granted = {str(r[0]).upper() for r in rows if r}
    missing = [v for v in _REQUIRED_VIEWS if v not in granted]
    if not missing:
        return CheckResult("LogMiner privileges", STATUS_PASS, "all required views are granted")
    return CheckResult(
        "LogMiner privileges", STATUS_WARN,
        # WARN, not FAIL: the grant may have come via a role, which
        # ALL_TAB_PRIVS doesn't show. Reporting this as a hard failure
        # would block plenty of correctly-configured databases.
        f"{len(missing)} view grant(s) not visible in ALL_TAB_PRIVS: {', '.join(missing)}. "
        "These may still be granted through a role, which this view doesn't show.",
        "\n".join(f"GRANT SELECT ON {v} TO {username.upper()};" for v in missing)
        + "\nGRANT LOGMINING TO " + username.upper() + ";")


def check_redo_retention(source) -> CheckResult:
    """Archive log deletion policy vs how long the bulk load will take.

    This is the check that exists specifically *because* the load is
    slow. Everything works until the load outruns retention, at which
    point the connector needs an archived log that has already been
    deleted and cannot resume -- forcing a fresh snapshot of a 1 TB
    source.
    """
    value = _scalar(source, "SELECT VALUE FROM V$PARAMETER WHERE NAME = 'db_flashback_retention_target'")
    if value is None:
        return CheckResult(
            "Redo/archive retention", STATUS_UNKNOWN,
            "could not read V$PARAMETER; confirm the archive log retention policy with the DBA",
            "Confirm archived redo is retained for longer than the expected bulk-load duration.")
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return CheckResult(
            "Redo/archive retention", STATUS_UNKNOWN, f"unrecognized retention value {value!r}")
    if minutes == 0:
        return CheckResult(
            "Redo/archive retention", STATUS_WARN,
            "flashback retention target is 0 (Oracle's default: archived redo is kept until "
            "backed up). Whether that's long enough depends entirely on the backup schedule -- "
            "confirm it exceeds the expected bulk-load duration.",
            "-- Confirm with the DBA that archived redo survives the full load window.")
    if minutes < MIN_RETENTION_MINUTES:
        return CheckResult(
            "Redo/archive retention", STATUS_WARN,
            f"retention target is {minutes} minutes ({minutes / 60:.1f}h). If the bulk load runs "
            "longer than that, the connector may need an archived log that has already been "
            "deleted and will be unable to resume without a fresh snapshot.",
            "ALTER SYSTEM SET DB_FLASHBACK_RETENTION_TARGET = 1440 SCOPE=BOTH;  -- 24h")
    return CheckResult(
        "Redo/archive retention", STATUS_PASS, f"retention target is {minutes} minutes")


def run_preflight(
    source, schema_name: str, table_names: List[str], username: Optional[str] = None,
) -> PreflightReport:
    """Run every check against a connected Oracle source connector.

    `source` needs only an `execute(sql)` returning row tuples -- the same
    duck-typed interface every SQL connector in this tool already has, so
    tests pass a fake catalog rather than needing an Oracle instance.
    """
    report = PreflightReport()
    report.results.append(check_archivelog_mode(source))
    report.results.append(check_minimal_supplemental_logging(source))
    report.results.append(check_table_supplemental_logging(source, schema_name, table_names))
    if username:
        report.results.append(check_logminer_privileges(source, username))
    report.results.append(check_redo_retention(source))
    return report
