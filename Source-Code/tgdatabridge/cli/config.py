"""Config-as-code for the headless CLI (ENTERPRISE_READINESS.md section 5,
item 2): a YAML/JSON migration definition (source, target, schema, table
selection, target engine) instead of a GUI session, so runs are
reproducible and reviewable in version control.

JSON always works (stdlib `json`, zero new dependency). YAML works too if
PyYAML happens to be installed (`import yaml`) -- consistent with this
project's existing preference for not adding a new runtime dependency
just for one optional convenience (see requirements.txt's ANTLR
vendored-fallback comment, and this module's own use of
urllib.request instead of adding `requests`). A .yaml/.yml file without
PyYAML installed fails with a clear, actionable error rather than a
confusing stack trace.

A config file never contains a plaintext password -- only the *name* of
an environment variable to read it from at run time
(`password_env`), so a config file is safe to commit to version control
right alongside the pipeline that runs it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from tgdatabridge.cdc import config as cdc_config
from tgdatabridge.core import sharding
from tgdatabridge.utils import app_storage
from tgdatabridge.core.connector_factory import FILE_SOURCE_ENGINES, SOURCE_ENGINES, TARGET_ENGINES
from tgdatabridge.db.access import ACCESS_DIRECT, MODE_FROM_TOKEN
from tgdatabridge.db.base import ConnectionParams
# Import-safe: spreadsheet_connector's own driver import (openpyxl) is
# lazy, inside connect(), so this costs nothing at config-load time and
# keeps the cap defined in exactly one place.
from tgdatabridge.db.spreadsheet_connector import MAX_SOURCE_FILES


class CliConfigError(Exception):
    """Raised for any problem loading or validating a job config -- an
    unreadable/malformed file, an unknown engine name, a missing
    password environment variable, and so on. The CLI entry point
    (tgdatabridge/cli/runner.py) catches this specifically and prints a clean,
    one-line message instead of a Python traceback, since a bad config
    file is an everyday user mistake, not a bug."""


@dataclass
class CliConnectionConfig:
    engine: str
    # For a file-based source engine (connector_factory.FILE_SOURCE_ENGINES,
    # i.e. "Excel/CSV"), `database` holds a **file path** and host/port/
    # username/password_env are all unused and default to empty -- there is
    # no server to reach or authenticate against. See _parse_connection.
    database: str
    host: str = ""
    port: int = 0
    username: str = ""
    password_env: str = ""
    schema: Optional[str] = None
    # Excel/CSV only: the full list of input files when the job reads more
    # than one (config key "files"). `database` still carries the first of
    # them, so anything reading a single path -- a log line, the checkpoint
    # key -- keeps working unchanged. Empty for a single-file job.
    files: List[str] = field(default_factory=list)
    # Excel/CSV only: name every table <file stem>_<sheet> rather than only
    # renaming on collision. None means "decide from the file count" --
    # prefix when there is more than one input file. See
    # spreadsheet_introspector._resolve_table_name.
    prefix_tables_with_file: Optional[bool] = None
    # SSH jump-host settings, as parsed from the "ssh" object. None means
    # a direct connection.
    ssh: Optional[Dict[str, Any]] = None
    # "public" | "ssh" | "vpn" -- see tgdatabridge/db/access.py.
    access: str = "public"
    # Certificate-based encryption (TLS/SSL), as parsed from the "tls"
    # object -- see _parse_tls and tgdatabridge/db/tls_config.py. None means
    # an unencrypted connection, exactly as before this existed. Composes
    # freely with `ssh` above.
    tls: Optional[Dict[str, Any]] = None


@dataclass
class CdcJobConfig:
    """Debezium change-data-capture settings (SCALE.md section 2.1).

    Only meaningful for an Oracle source. `enabled` is False by default:
    CDC is for the few-hour-cutover shape of migration, and a one-off
    copy shouldn't have to think about Kafka at all.
    """
    enabled: bool = False
    connect_url: str = "http://localhost:8083"
    kafka_bootstrap_servers: str = "localhost:9092"
    connector_name: str = "tgdatabridge-oracle-cdc"
    topic_prefix: str = "tgdatabridge"
    # no_data means Debezium streams changes only and this tool's own
    # Migrate Data step does the bulk load -- see tgdatabridge/cdc/config.py for
    # why that's the default and what ordering it implies.
    snapshot_mode: str = cdc_config.DEFAULT_SNAPSHOT_MODE
    log_mining_strategy: str = cdc_config.DEFAULT_LOG_MINING_STRATEGY
    database_pdb_name: Optional[str] = None
    # Seconds of lag at or below which the migration is considered
    # cutover-ready.
    drain_target_seconds: float = 60.0
    drain_timeout_seconds: float = 3600.0


@dataclass
class CliJobConfig:
    source: CliConnectionConfig
    target: CliConnectionConfig
    target_schema: Optional[str] = None
    # None (the default) means "every table in the loaded schema" --
    # matches the GUI's own checked_objects("Tables") or self.schema.tables
    # fallback-to-all behavior in main_window.py's _migrate_data.
    tables: Optional[List[str]] = None
    apply_ddl: bool = False
    migrate: bool = False
    # If migrate=True and this is also True, runs migrator.plan_schema
    # (tgdatabridge.core.migrator, section 2's dry-run/plan-mode feature) instead
    # of a real migrate_schema -- nothing is written to either side.
    dry_run_migrate: bool = False
    max_workers: int = 1
    # Intra-table sharding (tgdatabridge.core.sharding, SCALE.md section 1.2).
    # 0 means "use max_workers", which is the sensible default: there's no
    # point splitting a table into more pieces than there are workers to
    # run them. Only applies when max_workers > 1 -- sharding without
    # parallelism just turns one sequential scan into several.
    max_shards_per_table: int = 0
    min_rows_to_shard: int = sharding.DEFAULT_MIN_ROWS_TO_SHARD
    # Two-phase DDL (SCALE.md section 1.3). When True, "Apply DDL"
    # creates tables as bare columns and the constraints, indexes and
    # triggers are applied only after the data has landed -- the bulk
    # load then isn't maintaining an index per row, and triggers never
    # fire on migrated rows. Writes ddl_preload.sql/ddl_postload.sql
    # instead of a single ddl.sql.
    defer_constraints: bool = False
    # How often migration progress is written to the checkpoint file
    # (SCALE.md section 1.5). 0 writes after every batch, which is the
    # pre-throttling behaviour; the default coalesces those into one
    # write per interval. This is also the worst-case amount of
    # re-copied work if the process dies mid-run.
    checkpoint_flush_seconds: float = app_storage.DEFAULT_CHECKPOINT_FLUSH_SECONDS
    # ENTERPRISE_READINESS.md section 5, item 3: "Approval gate for
    # production". If True, apply_ddl/migrate are refused unless an
    # approver was given (--approved-by / TGDATABRIDGE_APPROVED_BY) and, if
    # approval_command is also set, that command exits 0.
    production: bool = False
    # An arbitrary shell command this org's own change-management tooling
    # provides -- e.g. one that checks a ticketing system and exits
    # non-zero if this migration isn't approved. This is the "integrated
    # with whatever change-management tool the org uses" hook from the
    # roadmap wording: deliberately generic rather than hardcoded to one
    # vendor, the same way log shipping (section 3, item 3) is a plain
    # HTTP endpoint rather than Splunk-specific code.
    approval_command: Optional[str] = None
    output_dir: str = "."
    cdc: CdcJobConfig = field(default_factory=CdcJobConfig)
    # Optional AI-assisted error diagnosis (tgdatabridge/ai/error_diagnostics.py),
    # as parsed from the top-level "ai" object -- see _parse_ai and
    # resolve_ai_config. None means no "ai" block at all, exactly like
    # `cdc`'s own convention -- the CLI is unattended by nature, so of the
    # four AI features in this tool, only error diagnosis (explain a
    # failure in the run's own output) fits it; schema mapping review,
    # plain-English requests and data quality review are interactive
    # review tools and stay GUI-only (see tgdatabridge/gui/ai_review_dialog.py).
    ai: Optional[Dict[str, Any]] = None


_KNOWN_CONNECTION_FIELDS = {
    "engine", "host", "port", "database", "username", "password_env", "schema",
    # Reach the database through an SSH bastion / jump host -- an object,
    # see _parse_ssh and tgdatabridge/db/ssh_tunnel.py. Never contains a secret:
    # the SSH password / key passphrase are named environment variables,
    # exactly like password_env above.
    "ssh",
    # "public" (default), "vpn", or "ssh". Optional: an "ssh" block on
    # its own already means "ssh", and no block at all means "public".
    # Spelling it out is for the VPN case, which has nothing to
    # configure but changes how a failure is reported -- see
    # tgdatabridge/db/access.py.
    "access",
    # Excel/CSV only -- a list of input paths, as an alternative to the
    # single "database" path. Rejected for every other engine by
    # _parse_connection, which only looks at it inside the
    # FILE_SOURCE_ENGINES branch.
    "files",
    "prefix_tables_with_file",
    # Certificate-based encryption (TLS/SSL) for the connection itself --
    # an object, see _parse_tls and tgdatabridge/db/tls_config.py. Never
    # contains a secret: the client private key's own passphrase (if any)
    # is a named environment variable, exactly like ssh's own secrets
    # above and password_env for the database password itself.
    "tls",
}
_KNOWN_JOB_FIELDS = {
    "source", "target", "target_schema", "tables", "apply_ddl", "migrate",
    "dry_run_migrate", "max_workers", "max_shards_per_table", "min_rows_to_shard",
    "defer_constraints", "checkpoint_flush_seconds",
    "production", "approval_command", "output_dir", "cdc", "ai",
}

_KNOWN_AI_FIELDS = {
    "enabled", "provider", "base_url", "model", "azure_api_version",
    "api_key_env", "error_diagnostics", "timeout_seconds",
}

_KNOWN_CDC_FIELDS = {
    "enabled", "connect_url", "kafka_bootstrap_servers", "connector_name", "topic_prefix",
    "snapshot_mode", "log_mining_strategy", "database_pdb_name",
    "drain_target_seconds", "drain_timeout_seconds",
}


def _parse_connection(raw: Dict[str, Any], which: str, valid_engines) -> CliConnectionConfig:
    if not isinstance(raw, dict):
        raise CliConfigError(f'"{which}" must be an object with engine/host/port/database/username/password_env.')

    unknown = set(raw) - _KNOWN_CONNECTION_FIELDS
    if unknown:
        raise CliConfigError(f'"{which}" has unknown field(s): {", ".join(sorted(unknown))}')

    if "engine" not in raw:
        raise CliConfigError(f'"{which}" is missing required field(s): engine')

    engine = raw["engine"]
    if engine not in valid_engines:
        raise CliConfigError(
            f'"{which}.engine" is "{engine}", which isn\'t one of the supported engines: {", ".join(valid_engines)}')

    # A file-based source ("Excel/CSV") reads a local path -- there's no
    # host to reach, no port to open and no credential to supply, so
    # requiring those fields would mean putting meaningless placeholder
    # values in every spreadsheet job config just to satisfy validation.
    if engine in FILE_SOURCE_ENGINES:
        if "database" not in raw and "files" not in raw:
            raise CliConfigError(
                f'"{which}" is missing required field(s): database (for engine "{engine}" this is '
                "the path to the .xlsx/.xlsm/.csv/.tsv file to read; use \"files\" with a list of "
                "paths to read several at once).")
        if "database" in raw and "files" in raw:
            raise CliConfigError(
                f'"{which}" sets both "database" and "files" -- give one or the other. Use '
                '"database" for a single file, "files" for a list.')
        for field_name in ("host", "port", "username", "password_env"):
            if field_name in raw:
                raise CliConfigError(
                    f'"{which}.{field_name}" doesn\'t apply to engine "{engine}", which reads a '
                    'local file -- remove it and leave only "engine", "database" (the file path) '
                    'or "files" (a list of paths), and optionally "schema".')

        files: List[str] = []
        if "files" in raw:
            value = raw["files"]
            if isinstance(value, str) or not isinstance(value, (list, tuple)):
                raise CliConfigError(
                    f'"{which}.files" must be a list of file paths. For a single file use '
                    '"database" instead.')
            files = [str(item).strip() for item in value if str(item).strip()]
            if not files:
                raise CliConfigError(f'"{which}.files" is empty -- list at least one file path.')
            if len(files) > MAX_SOURCE_FILES:
                raise CliConfigError(
                    f'"{which}.files" lists {len(files)} files, but at most {MAX_SOURCE_FILES} '
                    "can be read in one migration. Split the job into several runs.")

        prefix = raw.get("prefix_tables_with_file")
        if prefix is not None and not isinstance(prefix, bool):
            raise CliConfigError(
                f'"{which}.prefix_tables_with_file" must be true or false.')

        return CliConnectionConfig(
            engine=engine,
            database=str(raw["database"]) if "database" in raw else files[0],
            files=files,
            prefix_tables_with_file=prefix,
            schema=str(raw["schema"]) if raw.get("schema") else None,
        )

    for file_only in ("files", "prefix_tables_with_file"):
        if file_only in raw:
            raise CliConfigError(
                f'"{which}.{file_only}" only applies to a file-based source engine '
                f'({", ".join(FILE_SOURCE_ENGINES)}), not "{engine}".')

    missing = [f for f in ("host", "port", "database", "username", "password_env") if f not in raw]
    if missing:
        raise CliConfigError(f'"{which}" is missing required field(s): {", ".join(missing)}')

    try:
        port = int(raw["port"])
    except (TypeError, ValueError):
        raise CliConfigError(f'"{which}.port" must be a number, got {raw["port"]!r}.')

    return CliConnectionConfig(
        engine=engine, host=str(raw["host"]), port=port, database=str(raw["database"]),
        username=str(raw["username"]), password_env=str(raw["password_env"]),
        schema=str(raw["schema"]) if raw.get("schema") else None,
        ssh=_parse_ssh(raw.get("ssh"), which),
        access=_parse_access(raw.get("access"), which, raw.get("ssh")),
        tls=_parse_tls(raw.get("tls"), which),
    )


def _parse_access(raw: Any, which: str, ssh_block: Any) -> str:
    from tgdatabridge.db.access import MODE_FROM_TOKEN

    if raw is None:
        return "ssh" if ssh_block else "public"
    token = str(raw).strip().lower()
    if token not in MODE_FROM_TOKEN:
        raise CliConfigError(
            f'"{which}.access" is "{raw}", which isn\'t one of: '
            f'{", ".join(sorted(MODE_FROM_TOKEN))}.')
    if token == "ssh" and not ssh_block:
        raise CliConfigError(
            f'"{which}.access" is "ssh" but there is no "ssh" block to say which jump '
            "host to go through.")
    if token != "ssh" and ssh_block:
        raise CliConfigError(
            f'"{which}" has an "ssh" block but access is "{token}" -- set access to "ssh", '
            "or remove the block.")
    return token


_KNOWN_SSH_FIELDS = {
    "host", "port", "username", "auth_method", "private_key_path",
    "passphrase_env", "password_env", "database_host", "database_port",
    "verify_host_key",
}


def _parse_ssh(raw: Any, which: str) -> Optional[Dict[str, Any]]:
    """The optional "ssh" block: reach this database through a bastion.

    Same rule as the database password -- no secret is ever written in a
    config file. The SSH password and the key passphrase are given as the
    *names* of environment variables (password_env / passphrase_env), so
    the file stays safe to commit.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CliConfigError(
            f'"{which}.ssh" must be an object with host/username and either '
            'private_key_path or password_env.')
    unknown = set(raw) - _KNOWN_SSH_FIELDS
    if unknown:
        raise CliConfigError(
            f'"{which}.ssh" has unknown field(s): {", ".join(sorted(unknown))}')
    for required in ("host", "username"):
        if not str(raw.get(required, "")).strip():
            raise CliConfigError(f'"{which}.ssh" is missing required field(s): {required}')
    if not raw.get("private_key_path") and not raw.get("password_env"):
        raise CliConfigError(
            f'"{which}.ssh" needs either "private_key_path" or "password_env" -- there is '
            "no way to authenticate to the jump host otherwise.")
    for numeric in ("port", "database_port"):
        if numeric in raw:
            try:
                int(raw[numeric])
            except (TypeError, ValueError):
                raise CliConfigError(
                    f'"{which}.ssh.{numeric}" must be a number, got {raw[numeric]!r}.')
    return dict(raw)


_KNOWN_TLS_FIELDS = {
    "verify_cert", "verify_hostname", "ca_cert_path",
    "client_cert_path", "client_key_path", "client_key_password_env",
}


def _parse_tls(raw: Any, which: str) -> Optional[Dict[str, Any]]:
    """The optional "tls" block: encrypt this connection with a
    certificate, enterprise-grade -- see tgdatabridge/db/tls_config.py.

    Presence of the block is what turns TLS on, the same convention the
    "ssh" block already uses for the tunnel. Same rule as the database
    password and the SSH secrets above: no secret is ever written in a
    config file -- a client private key's passphrase (only meaningful
    together with client_cert_path/client_key_path, for mutual TLS) is
    given as the *name* of an environment variable
    (client_key_password_env), so the file stays safe to commit.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CliConfigError(
            f'"{which}.tls" must be an object -- an empty one ({{}}) already means '
            '"encrypted, verify the certificate", which is the default enterprise-grade '
            "setting.")
    unknown = set(raw) - _KNOWN_TLS_FIELDS
    if unknown:
        raise CliConfigError(
            f'"{which}.tls" has unknown field(s): {", ".join(sorted(unknown))}')
    for flag in ("verify_cert", "verify_hostname"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise CliConfigError(f'"{which}.tls.{flag}" must be true or false.')
    if bool(raw.get("client_cert_path")) != bool(raw.get("client_key_path")):
        raise CliConfigError(
            f'"{which}.tls" needs both "client_cert_path" and "client_key_path" for mutual '
            "TLS, or neither -- only one of the two was given.")
    if raw.get("verify_hostname", True) and not raw.get("verify_cert", True):
        raise CliConfigError(
            f'"{which}.tls.verify_hostname" is true but "verify_cert" is false -- verifying '
            "the hostname requires verifying the certificate too.")
    return dict(raw)


def _parse_cdc(raw: Any, source_engine: str) -> CdcJobConfig:
    if raw is None:
        return CdcJobConfig()
    if not isinstance(raw, dict):
        raise CliConfigError('"cdc" must be an object.')

    unknown = set(raw) - _KNOWN_CDC_FIELDS
    if unknown:
        raise CliConfigError(f'"cdc" has unknown field(s): {", ".join(sorted(unknown))}')

    enabled = bool(raw.get("enabled", False))
    # Debezium's Oracle connector is the only capture path this tool
    # generates config for, so enabling CDC against any other source is a
    # mistake worth catching in the config rather than at connector-start
    # time on the cluster.
    if enabled and source_engine != "Oracle":
        raise CliConfigError(
            f'"cdc.enabled" is true but the source engine is "{source_engine}". CDC integration '
            "currently covers the Debezium Oracle connector only.")

    snapshot_mode = str(raw.get("snapshot_mode", cdc_config.DEFAULT_SNAPSHOT_MODE))
    if snapshot_mode not in cdc_config.SNAPSHOT_MODES:
        raise CliConfigError(
            f'"cdc.snapshot_mode" is "{snapshot_mode}", which isn\'t one of: '
            f'{", ".join(cdc_config.SNAPSHOT_MODES)}.')

    strategy = str(raw.get("log_mining_strategy", cdc_config.DEFAULT_LOG_MINING_STRATEGY))
    if strategy not in cdc_config.LOG_MINING_STRATEGIES:
        raise CliConfigError(
            f'"cdc.log_mining_strategy" is "{strategy}", which isn\'t one of: '
            f'{", ".join(cdc_config.LOG_MINING_STRATEGIES)}.')

    def _positive_float(key, default):
        value = raw.get(key, default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise CliConfigError(f'"cdc.{key}" must be a number, got {raw.get(key)!r}.')
        if value < 0:
            raise CliConfigError(f'"cdc.{key}" cannot be negative.')
        return value

    return CdcJobConfig(
        enabled=enabled,
        connect_url=str(raw.get("connect_url", CdcJobConfig.connect_url)),
        kafka_bootstrap_servers=str(
            raw.get("kafka_bootstrap_servers", CdcJobConfig.kafka_bootstrap_servers)),
        connector_name=str(raw.get("connector_name", CdcJobConfig.connector_name)),
        topic_prefix=str(raw.get("topic_prefix", CdcJobConfig.topic_prefix)),
        snapshot_mode=snapshot_mode,
        log_mining_strategy=strategy,
        database_pdb_name=str(raw["database_pdb_name"]) if raw.get("database_pdb_name") else None,
        drain_target_seconds=_positive_float("drain_target_seconds", 60.0),
        drain_timeout_seconds=_positive_float("drain_timeout_seconds", 3600.0),
    )


def _parse_ai(raw: Any) -> Optional[Dict[str, Any]]:
    """The optional top-level "ai" block: turn on AI-assisted error
    diagnosis for this run -- see tgdatabridge/ai/ and resolve_ai_config
    below, which turns this raw dict into a real AiConfig at run time
    (resolving `api_key_env` from the environment the same way every
    other secret in this config format is resolved).

    `None` (no "ai" block at all) is kept distinct from an "ai" block
    with `enabled` left at its default False -- same convention as
    _parse_tls's own docstring explains for its block, and for the same
    reason: resolve_ai_config needs to tell "nothing configured" apart
    from "configured but switched off" only insofar as both mean "don't
    call out to an AI provider", so in practice this module treats them
    the same; the distinction is kept anyway so a future feature reading
    this dict (unlikely, but tls.py's own history is exactly why this
    isn't assumed) does not have to guess."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CliConfigError('"ai" must be an object.')

    unknown = set(raw) - _KNOWN_AI_FIELDS
    if unknown:
        raise CliConfigError(f'"ai" has unknown field(s): {", ".join(sorted(unknown))}')

    from tgdatabridge.ai.ai_config import PROVIDERS
    provider = str(raw.get("provider", "claude"))
    if provider not in PROVIDERS:
        raise CliConfigError(
            f'"ai.provider" is "{provider}", which isn\'t one of: {", ".join(PROVIDERS)}.')

    return dict(raw)


def parse_job_config(raw: Dict[str, Any]) -> CliJobConfig:
    """Validates and converts an already-parsed dict (from JSON or YAML)
    into a CliJobConfig. Split out from load_job_config() so tests (and
    any future embedder) can build a config from an in-memory dict without
    needing a real file on disk."""
    if not isinstance(raw, dict):
        raise CliConfigError("The top level of a migration config file must be an object.")

    unknown = set(raw) - _KNOWN_JOB_FIELDS
    if unknown:
        raise CliConfigError(f"Unknown top-level field(s): {', '.join(sorted(unknown))}")

    if "source" not in raw or "target" not in raw:
        raise CliConfigError('A migration config needs both a "source" and a "target" connection.')

    source = _parse_connection(raw["source"], "source", SOURCE_ENGINES)
    target = _parse_connection(raw["target"], "target", TARGET_ENGINES)

    tables = raw.get("tables")
    if tables is not None:
        if not isinstance(tables, list) or not all(isinstance(t, str) for t in tables):
            raise CliConfigError('"tables", if given, must be a list of table name strings.')

    max_workers = raw.get("max_workers", 1)
    try:
        max_workers = int(max_workers)
    except (TypeError, ValueError):
        raise CliConfigError(f'"max_workers" must be a number, got {max_workers!r}.')
    if max_workers < 1:
        raise CliConfigError('"max_workers" must be at least 1.')

    max_shards_per_table = raw.get("max_shards_per_table", 0)
    try:
        max_shards_per_table = int(max_shards_per_table)
    except (TypeError, ValueError):
        raise CliConfigError(f'"max_shards_per_table" must be a number, got {max_shards_per_table!r}.')
    if max_shards_per_table < 0:
        raise CliConfigError('"max_shards_per_table" cannot be negative (0 means "use max_workers").')

    checkpoint_flush_seconds = raw.get(
        "checkpoint_flush_seconds", app_storage.DEFAULT_CHECKPOINT_FLUSH_SECONDS)
    try:
        checkpoint_flush_seconds = float(checkpoint_flush_seconds)
    except (TypeError, ValueError):
        raise CliConfigError(
            f'"checkpoint_flush_seconds" must be a number, got {checkpoint_flush_seconds!r}.')
    if checkpoint_flush_seconds < 0:
        raise CliConfigError('"checkpoint_flush_seconds" cannot be negative (0 means "every batch").')

    min_rows_to_shard = raw.get("min_rows_to_shard", sharding.DEFAULT_MIN_ROWS_TO_SHARD)
    try:
        min_rows_to_shard = int(min_rows_to_shard)
    except (TypeError, ValueError):
        raise CliConfigError(f'"min_rows_to_shard" must be a number, got {min_rows_to_shard!r}.')
    if min_rows_to_shard < 0:
        raise CliConfigError('"min_rows_to_shard" cannot be negative.')

    approval_command = raw.get("approval_command")
    if approval_command is not None and not isinstance(approval_command, str):
        raise CliConfigError('"approval_command" must be a string (a shell command).')

    return CliJobConfig(
        source=source, target=target,
        target_schema=str(raw["target_schema"]) if raw.get("target_schema") else None,
        tables=list(tables) if tables is not None else None,
        apply_ddl=bool(raw.get("apply_ddl", False)),
        migrate=bool(raw.get("migrate", False)),
        dry_run_migrate=bool(raw.get("dry_run_migrate", False)),
        max_workers=max_workers,
        max_shards_per_table=max_shards_per_table,
        min_rows_to_shard=min_rows_to_shard,
        defer_constraints=bool(raw.get("defer_constraints", False)),
        checkpoint_flush_seconds=checkpoint_flush_seconds,
        cdc=_parse_cdc(raw.get("cdc"), source.engine),
        production=bool(raw.get("production", False)),
        approval_command=approval_command,
        output_dir=str(raw.get("output_dir", ".")),
        ai=_parse_ai(raw.get("ai")),
    )


def load_job_config(path: Path) -> CliJobConfig:
    """Reads and validates a migration config file. `.json` is always
    supported; `.yaml`/`.yml` needs PyYAML installed (a clear
    CliConfigError is raised otherwise, naming the two ways to fix it)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliConfigError(f"Could not read config file {path}: {exc}") from exc

    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise CliConfigError(
                f"{path} is a YAML file, but PyYAML isn't installed in this environment. "
                f'Run "pip install pyyaml", or write the config as JSON instead (.json).'
            ) from exc
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise CliConfigError(f"Could not parse {path} as YAML: {exc}") from exc
    else:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CliConfigError(f"Could not parse {path} as JSON: {exc}") from exc

    return parse_job_config(raw)


def resolve_connection_params(conn: CliConnectionConfig) -> ConnectionParams:
    """Reads the actual password from the environment variable named by
    `conn.password_env` -- the config file itself never contains a
    password, only this pointer to where to find one at run time (kept
    out of shell history / CI logs the same way any secret-bearing env
    var is).

    A file-based source engine has no password_env at all (see
    _parse_connection), so it skips the lookup entirely rather than
    failing on a missing environment variable that was never meant to
    exist."""
    if conn.engine in FILE_SOURCE_ENGINES:
        return ConnectionParams(
            host="", port=0, database=conn.database, username="", password="", schema=conn.schema,
            files=list(conn.files) or None,
            prefix_tables_with_file=conn.prefix_tables_with_file,
        )

    password = os.environ.get(conn.password_env)
    if password is None:
        raise CliConfigError(
            f'Environment variable "{conn.password_env}" (referenced by password_env) is not set.')
    return ConnectionParams(
        host=conn.host, port=conn.port, database=conn.database,
        username=conn.username, password=password, schema=conn.schema,
        ssh=_ssh_params(conn),
        access_mode=MODE_FROM_TOKEN.get(conn.access, ACCESS_DIRECT),
        tls=_tls_params(conn),
    )


def _ssh_params(conn: CliConnectionConfig):
    """Build the tunnel config, resolving the two secrets from the
    environment the same way the database password is resolved above."""
    raw = conn.ssh
    if not raw:
        return None
    from tgdatabridge.db.ssh_tunnel import AUTH_KEY, AUTH_PASSWORD, SshTunnelConfig

    def from_env(name_key: str) -> str:
        env_name = str(raw.get(name_key, "") or "")
        if not env_name:
            return ""
        value = os.environ.get(env_name)
        if value is None:
            raise CliConfigError(
                f'Environment variable "{env_name}" (referenced by ssh.{name_key}) is not set.')
        return value

    method = str(raw.get("auth_method") or "").strip()
    if method not in (AUTH_KEY, AUTH_PASSWORD):
        method = AUTH_KEY if raw.get("private_key_path") else AUTH_PASSWORD
    return SshTunnelConfig(
        enabled=True,
        host=str(raw.get("host", "")),
        port=int(raw.get("port", 22) or 22),
        username=str(raw.get("username", "")),
        auth_method=method,
        private_key_path=str(raw.get("private_key_path", "") or ""),
        private_key_passphrase=from_env("passphrase_env"),
        password=from_env("password_env"),
        remote_host=str(raw.get("database_host", "") or ""),
        remote_port=int(raw.get("database_port", 0) or 0),
        verify_host_key=bool(raw.get("verify_host_key", False)),
    )


def _tls_params(conn: CliConnectionConfig):
    """Build the TlsConfig, resolving the client key passphrase from the
    environment the same way _ssh_params resolves its own secrets.

    `is None`, not a falsy check: an empty "tls" object ({}) is a valid,
    deliberate config -- "encrypted, verify the certificate" with every
    field at its secure default -- and must not be treated the same as no
    "tls" block at all. See _parse_tls's own docstring.
    """
    raw = conn.tls
    if raw is None:
        return None
    from tgdatabridge.db.tls_config import TlsConfig

    env_name = str(raw.get("client_key_password_env", "") or "")
    client_key_password = ""
    if env_name:
        value = os.environ.get(env_name)
        if value is None:
            raise CliConfigError(
                f'Environment variable "{env_name}" (referenced by tls.client_key_password_env) '
                "is not set.")
        client_key_password = value

    return TlsConfig(
        enabled=True,
        verify_cert=bool(raw.get("verify_cert", True)),
        verify_hostname=bool(raw.get("verify_hostname", True)),
        ca_cert_path=str(raw.get("ca_cert_path", "") or ""),
        client_cert_path=str(raw.get("client_cert_path", "") or ""),
        client_key_path=str(raw.get("client_key_path", "") or ""),
        client_key_password=client_key_password,
    )


def resolve_ai_config(config: CliJobConfig):
    """Build a real tgdatabridge.ai.ai_config.AiConfig from
    config.ai (the parsed "ai" block), resolving `api_key_env` from the
    environment exactly the way every other secret-bearing field in this
    config format is resolved (see resolve_connection_params's own
    password lookup). Returns None if there is no "ai" block, or if
    "ai.enabled" is left at its default False -- tgdatabridge/cli/runner.py
    treats both identically ("don't call out to an AI provider for this
    run") and does not need to tell them apart, so this collapses them
    into the one signal it actually acts on."""
    raw = config.ai
    if not raw or not raw.get("enabled", False):
        return None

    from tgdatabridge.ai.ai_config import AiConfig

    api_key = ""
    env_name = str(raw.get("api_key_env", "") or "")
    if env_name:
        value = os.environ.get(env_name)
        if value is None:
            raise CliConfigError(
                f'Environment variable "{env_name}" (referenced by ai.api_key_env) is not set.')
        api_key = value

    return AiConfig(
        enabled=True,
        provider=str(raw.get("provider", "claude")),
        api_key=api_key,
        base_url=str(raw.get("base_url", "") or ""),
        model=str(raw.get("model", "") or ""),
        azure_api_version=str(raw.get("azure_api_version", "2024-06-01") or "2024-06-01"),
        timeout_seconds=float(raw.get("timeout_seconds", 60.0) or 60.0),
        feature_error_diagnostics=bool(raw.get("error_diagnostics", True)),
    )
