"""Generates a Debezium Oracle connector configuration for Kafka Connect
from a schema this tool has already introspected -- SCALE.md section 2.1.

Why generate it rather than hand-write it
------------------------------------------
The connector's `table.include.list` has to name exactly the tables being
migrated, in Oracle's own casing, schema-qualified. That list already
exists in memory the moment `Load Schema` finishes, and hand-maintaining
a second copy of it in a YAML file is how a table quietly ends up
uncaptured -- migrated by the bulk load, then silently diverging from the
source for the rest of the cutover window. Deriving it from the same
`Schema` object the migration uses removes that whole class of mistake.

The snapshot mode is the integration decision
----------------------------------------------
Debezium can take its own initial snapshot (`snapshot.mode=initial`), but
for a large migration that throws away this tool's parallel, sharded,
COPY-based bulk load and replaces it with a considerably slower one. The
default here is therefore `no_data`: Debezium captures the table
structures and starts streaming from the current SCN **without** reading
any existing rows, and this tool's own migrator does the bulk load.

That split makes the *ordering* load-bearing, and it's the one thing that
silently loses data if you get it wrong:

    1. Register the connector first, in no_data mode. It records its
       starting SCN and begins buffering changes into Kafka.
    2. Only then start the bulk load.
    3. Let the sink drain the buffered changes; cut over when lag is
       small.

Starting the bulk load *before* the connector leaves a gap: any change
committed between the load's read-consistent point and the connector's
start SCN is captured by neither. Nothing in Debezium or in this tool
would report that gap -- the row counts would even match -- so
`orchestrator.py` owns this sequence rather than leaving it to a runbook.

Passwords
---------
Never written into the generated JSON. The config carries a
`${file:...}` / env-var reference exactly the way the CLI's own configs
carry `password_env`, so a generated connector file is safe to commit
next to the pipeline that posts it. See `render_config`'s
`password_mode`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tgdatabridge.core.schema_model import Schema, Table

ORACLE_CONNECTOR_CLASS = "io.debezium.connector.oracle.OracleConnector"

# Debezium's own snapshot.mode vocabulary. "no_data" is what this tool
# defaults to (it does its own bulk load); "initial" hands the snapshot to
# Debezium instead. The deprecated spellings ("schema_only",
# "schema_only_recovery") are deliberately absent -- generating a config
# with a deprecated value would produce a warning on every connector start
# for no benefit.
SNAPSHOT_MODES = (
    "no_data", "initial", "initial_only", "always", "when_needed", "recovery",
)
DEFAULT_SNAPSHOT_MODE = "no_data"

# log.mining.strategy. online_catalog is Debezium's own default and the
# cheapest; redo_log_catalog is the reliable-under-DDL-churn option;
# hybrid aims for both. A migration cutover window is normally a DDL
# freeze, which is why the cheap default is the right one here.
LOG_MINING_STRATEGIES = ("online_catalog", "redo_log_catalog", "hybrid")
DEFAULT_LOG_MINING_STRATEGY = "online_catalog"

PASSWORD_MODE_ENV = "env"
PASSWORD_MODE_FILE = "file"
PASSWORD_MODE_PLAIN = "plain"


class CdcConfigError(Exception):
    """Raised for a CDC configuration that can't be generated -- an
    unknown snapshot mode, a schema with no capturable tables, and so on.
    The CLI catches this and prints one clean line, the same way it
    handles CliConfigError."""


@dataclass
class DebeziumConnectorConfig:
    """Everything needed to render one Debezium Oracle connector."""

    connector_name: str
    database_hostname: str
    database_port: int
    database_user: str
    database_dbname: str
    topic_prefix: str
    kafka_bootstrap_servers: str

    # Oracle multitenant: when the source is a PDB, database.dbname is the
    # CDB and database.pdb.name is the PDB. Leaving pdb unset is the
    # correct configuration for a non-CDB source, not an omission.
    database_pdb_name: Optional[str] = None

    schema_name: str = ""
    table_names: List[str] = field(default_factory=list)

    snapshot_mode: str = DEFAULT_SNAPSHOT_MODE
    log_mining_strategy: str = DEFAULT_LOG_MINING_STRATEGY

    password_mode: str = PASSWORD_MODE_ENV
    password_env: str = "SRC_DB_PASSWORD"
    password_value: str = ""

    schema_history_topic: Optional[str] = None
    tasks_max: int = 1
    # Extra raw Debezium properties merged in last, so anything this
    # module doesn't model can still be set without patching it.
    extra: Dict[str, Any] = field(default_factory=dict)


def _password_reference(config: DebeziumConnectorConfig) -> str:
    if config.password_mode == PASSWORD_MODE_PLAIN:
        return config.password_value
    if config.password_mode == PASSWORD_MODE_FILE:
        # Kafka Connect's FileConfigProvider. Requires
        # config.providers=file on the worker; noted in the generated
        # file's companion README rather than silently assumed.
        return "${file:/opt/kafka/external-secrets.properties:" + config.password_env + "}"
    return "${env:" + config.password_env + "}"


def capturable_tables(schema: Schema) -> List[Table]:
    """Tables Debezium can actually capture from an Oracle source.

    A synthesized child table (MongoDB array unwind) or a spreadsheet
    sheet has no Oracle table behind it to mine redo for, so including it
    would produce an include-list entry Debezium silently never matches --
    the worst kind of wrong, since the connector starts cleanly and just
    never emits those rows.
    """
    return [
        t for t in schema.tables
        if getattr(t, "source_array_path", None) is None
    ]


def config_from_schema(
    schema: Schema,
    connector_name: str,
    database_hostname: str,
    database_port: int,
    database_user: str,
    database_dbname: str,
    topic_prefix: str,
    kafka_bootstrap_servers: str,
    table_names: Optional[List[str]] = None,
    **overrides,
) -> DebeziumConnectorConfig:
    """Build a connector config whose include list comes from the same
    Schema object the migration itself uses.

    `table_names`, if given, narrows the capture set to those tables (the
    CLI passes its own `tables` selection through, so a partial migration
    doesn't capture changes for tables it isn't migrating).
    """
    tables = capturable_tables(schema)
    if table_names:
        wanted = {name.lower() for name in table_names}
        tables = [t for t in tables if t.name.lower() in wanted]

    if not tables:
        raise CdcConfigError(
            "No capturable tables: the schema has no Oracle tables to mine redo for "
            "(or the table filter excluded all of them). A CDC connector with an empty "
            "include list would start cleanly and capture nothing at all."
        )

    return DebeziumConnectorConfig(
        connector_name=connector_name,
        database_hostname=database_hostname,
        database_port=database_port,
        database_user=database_user,
        database_dbname=database_dbname,
        topic_prefix=topic_prefix,
        kafka_bootstrap_servers=kafka_bootstrap_servers,
        schema_name=schema.name,
        table_names=[t.name for t in tables],
        **overrides,
    )


def render_config(config: DebeziumConnectorConfig) -> Dict[str, Any]:
    """The `config` object of a Kafka Connect connector -- i.e. what goes
    under the "config" key of a POST to /connectors."""
    if config.snapshot_mode not in SNAPSHOT_MODES:
        raise CdcConfigError(
            f'snapshot_mode "{config.snapshot_mode}" is not one of: {", ".join(SNAPSHOT_MODES)}.')
    if config.log_mining_strategy not in LOG_MINING_STRATEGIES:
        raise CdcConfigError(
            f'log_mining_strategy "{config.log_mining_strategy}" is not one of: '
            f'{", ".join(LOG_MINING_STRATEGIES)}.')
    if config.password_mode not in (PASSWORD_MODE_ENV, PASSWORD_MODE_FILE, PASSWORD_MODE_PLAIN):
        raise CdcConfigError(f'Unknown password_mode "{config.password_mode}".')
    if not config.table_names:
        raise CdcConfigError(
            "table.include.list would be empty -- the connector would start cleanly and "
            "capture nothing.")

    schema_prefix = config.schema_name.upper()
    include_list = ",".join(f"{schema_prefix}.{name.upper()}" for name in sorted(config.table_names))
    history_topic = config.schema_history_topic or f"schema-history.{config.topic_prefix}"

    rendered: Dict[str, Any] = {
        "connector.class": ORACLE_CONNECTOR_CLASS,
        "tasks.max": str(config.tasks_max),
        "database.hostname": config.database_hostname,
        "database.port": str(config.database_port),
        "database.user": config.database_user,
        "database.password": _password_reference(config),
        "database.dbname": config.database_dbname,
        # The logical name every emitted topic is prefixed with. Changing
        # it later orphans the existing topics and offsets, so it's worth
        # treating as permanent for a given migration.
        "topic.prefix": config.topic_prefix,
        "schema.include.list": schema_prefix,
        "table.include.list": include_list,
        "snapshot.mode": config.snapshot_mode,
        "log.mining.strategy": config.log_mining_strategy,
        "schema.history.internal.kafka.bootstrap.servers": config.kafka_bootstrap_servers,
        "schema.history.internal.kafka.topic": history_topic,
        # Only the captured tables' DDL is stored, keeping the history
        # topic proportional to the migration rather than to the whole
        # database.
        "schema.history.internal.store.only.captured.tables.ddl": "true",
    }

    if config.database_pdb_name:
        rendered["database.pdb.name"] = config.database_pdb_name

    rendered.update({str(k): v for k, v in config.extra.items()})
    return rendered


def render_connector_document(config: DebeziumConnectorConfig) -> Dict[str, Any]:
    """The full `{"name": ..., "config": {...}}` document a POST to
    /connectors takes, and what gets written to disk as
    debezium-connector.json."""
    return {"name": config.connector_name, "config": render_config(config)}


def to_json(config: DebeziumConnectorConfig) -> str:
    return json.dumps(render_connector_document(config), indent=2, sort_keys=False)


def ordering_notes(config: DebeziumConnectorConfig) -> List[str]:
    """Human-readable warnings about the load/capture handoff, written
    alongside the generated config.

    These aren't decoration: the no_data ordering constraint is the one
    part of this whole integration where a mistake loses data silently
    and no row count would reveal it.
    """
    notes: List[str] = []
    if config.snapshot_mode in ("no_data", "recovery"):
        notes.append(
            "snapshot.mode is '{}': Debezium will NOT copy existing rows -- this tool's own "
            "Migrate Data step does that.".format(config.snapshot_mode))
        notes.append(
            "ORDER MATTERS: register this connector BEFORE starting the bulk load. The "
            "connector records its start SCN on registration and buffers everything after it. "
            "Loading first leaves a gap -- changes committed between the load's read point and "
            "the connector's start SCN are captured by neither, and row counts will still match, "
            "so nothing would flag it.")
    elif config.snapshot_mode in ("initial", "initial_only", "always"):
        notes.append(
            "snapshot.mode is '{}': Debezium takes its own initial snapshot, so do NOT also run "
            "this tool's Migrate Data step for these tables -- you would load every row twice. "
            "Note this also gives up the parallel/sharded COPY path, which is considerably "
            "faster for a large source.".format(config.snapshot_mode))
    if config.tasks_max != 1:
        notes.append(
            "The Oracle connector only ever uses a single task regardless of tasks.max; the "
            "value above is harmless but has no effect.")
    return notes
