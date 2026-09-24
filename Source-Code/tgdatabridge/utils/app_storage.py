"""Local, on-disk persistence for saved connection profiles and past
conversion runs -- entirely separate from tgdatabridge.utils.logger's in-memory
session log (which resets every time the app restarts).

Everything lives under one per-user application-data directory:

    Windows:      %APPDATA%\\TGDataBridge\\
    macOS/Linux:  ~/.tg_databridge/    (a conventional dotfile fallback --
                                          this is a Windows-first desktop
                                          tool but developed/tested
                                          cross-platform)

The rebrand from "Teleglobal Database Migration Tool" to TG DataBridge
changed that directory's name. An existing install's contents are copied
across from the old %APPDATA%\\TeleglobalTDMT\\ once, on first run -- see
_adopt_legacy_dir -- so saved connections, run history and any in-flight
migration checkpoint survive the rename.

Two flat JSON files inside it:

    connection_profiles.json   Saved source/target connection profiles.
                                Deliberately NEVER includes a password --
                                see ConnectionProfile's docstring for why.
    conversion_history.json    A capped, most-recent-first log of past
                                "Convert Schema" runs. Each record can
                                optionally point at an auto-saved copy of
                                that run's report/DDL under history_files/,
                                so the History dialog can reopen them later
                                even if the user never clicked
                                "Save Report.../Save DDL..." themselves.

Plus one directory of small per-migration files:

    migration_checkpoints/<id>.json   Resumable progress for one logical
                                migration (a specific source database,
                                target database, schema, and target
                                engine combination) -- see
                                MigrationCheckpoint below and
                                tgdatabridge.core.migrator's checkpoint/resume
                                support. Unlike conversion_history.json,
                                these are deleted once a migration
                                finishes with nothing failed -- there is
                                nothing left to ever resume.

Every public function accepts an optional `base_dir` override (defaulting
to the real per-user app-data directory) purely so tests can point this
module at a throwaway temp directory instead of touching the real user
profile.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from tgdatabridge import version
from tgdatabridge.utils.logger import current_actor

_APP_DIR_NAME = version.SLUG                      # "TGDataBridge"
_LEGACY_APP_DIR_NAME = version.LEGACY_APP_DIR_NAME  # "TeleglobalTDMT"
_DOT_DIR_NAME = ".tg_databridge"
_LEGACY_DOT_DIR_NAME = ".teleglobal_tdmt"
_MAX_PROFILES_PER_ENGINE = 20
_MAX_HISTORY_RECORDS = 200
_CHECKPOINTS_DIR_NAME = "migration_checkpoints"


def local_app_data_dir(base_dir: Optional[Path] = None) -> Path:
    """The real, always-per-machine directory -- regardless of any
    configured shared_storage_path (ENTERPRISE_READINESS.md section 5,
    item 4). settings.json itself always lives here: it's what *stores*
    shared_storage_path, so it can't be resolved through the very setting
    it's holding, or every machine's app would need to already know the
    shared path before it could read the file that tells it the shared
    path. Every other public function in this module goes through
    app_data_dir() below instead, which reads this file to decide whether
    to redirect."""
    if base_dir is not None:
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir

    appdata = os.environ.get("APPDATA")  # set on Windows
    if appdata:
        directory = Path(appdata) / _APP_DIR_NAME
        legacy = Path(appdata) / _LEGACY_APP_DIR_NAME
    else:
        directory = Path.home() / _DOT_DIR_NAME
        legacy = Path.home() / _LEGACY_DOT_DIR_NAME

    _adopt_legacy_dir(legacy, directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _adopt_legacy_dir(legacy: Path, current: Path) -> None:
    """Carry a pre-rebrand install's data over to the new directory, once.

    The product was renamed; the user's saved connection profiles, run
    history, migration checkpoints and sync watermarks were not. Pointing
    the new name at a fresh empty folder would look, from the outside,
    exactly like the rebrand had wiped them -- and a checkpoint lost
    mid-migration is not a cosmetic loss: the next run would re-copy every
    table that had already landed.

    Copied rather than moved, and only when the new directory does not
    exist yet, so this is a one-time, non-destructive adoption: the old
    folder is left exactly where it is, and an older build of the tool
    installed alongside this one keeps working from it.

    Every failure here is swallowed. Not being able to inherit the old
    settings is an inconvenience; failing to start because of it is not
    acceptable, and the caller immediately creates the directory anyway.
    """
    try:
        if current.exists() or not legacy.is_dir():
            return
        shutil.copytree(legacy, current)
    except (OSError, shutil.Error):
        # A partially copied directory is still better than none: whatever
        # landed is valid JSON in its own right, and anything missing is
        # re-created on demand.
        pass


def _configured_shared_dir() -> Optional[Path]:
    """Best-effort read of settings.json's shared_storage_path, without
    importing tgdatabridge.utils.settings (that module imports this one for its
    own storage location, and reading the *shared* path is itself
    settings-shaped state that needs to stay independent of it, hence the
    small amount of duplicated JSON-reading here rather than a shared
    helper). Any problem at all -- missing file, corrupted JSON, an
    unreachable network path -- falls back to None (use the local
    directory) rather than ever raising; a team's shared drive being
    briefly offline should degrade to "acts like a single-user install
    again", not crash the app."""
    try:
        settings_path = local_app_data_dir() / "settings.json"
        if not settings_path.exists():
            return None
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
        shared = raw.get("shared_storage_path") if isinstance(raw, dict) else None
        if not shared or not str(shared).strip():
            return None
        path = Path(str(shared).strip())
        path.mkdir(parents=True, exist_ok=True)
        return path
    except (OSError, ValueError):
        return None


def app_data_dir(base_dir: Optional[Path] = None) -> Path:
    """The directory everything in this module (other than settings.json
    itself -- see local_app_data_dir) reads/writes under. `base_dir`, if
    given, is used as-is (and created if missing) -- this is the
    test-injection seam; real callers should never pass it.

    Otherwise: if a shared_storage_path has been configured (Settings…,
    ENTERPRISE_READINESS.md section 5, item 4 -- "let an admin point
    app_storage's base_dir at a shared network location"), every profile/
    history/checkpoint/log/metrics file redirects there instead of the
    per-machine directory, so a team sharing that location shares all of
    it. Falls back to the ordinary per-machine directory when nothing is
    configured, or if the configured location can't be reached right now."""
    if base_dir is not None:
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir

    shared = _configured_shared_dir()
    if shared is not None:
        return shared
    return local_app_data_dir()


def _read_json_list(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        # A corrupted or unreadable file shouldn't crash the app on
        # startup -- treat it as empty and let subsequent saves overwrite it.
        return []


def _write_json_list(path: Path, items: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2)


# ------------------------------------------------------- connection profiles


@dataclass
class ConnectionProfile:
    """A saved connection's non-secret fields. The password is
    deliberately never part of this dataclass or persisted anywhere -- it's
    always re-entered by the user, so nothing sensitive ever touches disk."""
    engine: str
    host: str
    port: int
    database: str
    username: str
    schema: Optional[str] = None

    # SSH jump-host settings, when this connection reaches its database
    # through a bastion (see tgdatabridge/db/ssh_tunnel.py). Non-secret parts
    # only, on exactly the same principle as the database password above:
    # the SSH password and the key's passphrase are never written here,
    # only the path to the key file. All optional, so a profile saved by
    # an earlier version loads unchanged.
    ssh_enabled: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_username: str = ""
    ssh_auth_method: str = ""
    ssh_private_key_path: str = ""
    ssh_remote_host: str = ""
    ssh_remote_port: int = 0
    ssh_verify_host_key: bool = False

    # "public" | "ssh" | "vpn" -- how this connection reaches its database
    # (tgdatabridge/db/access.py). A short token rather than the dialog's display
    # label, so the wording can change without invalidating what is on
    # disk. Empty means a profile saved before this existed: it is read as
    # "ssh" when ssh_enabled is set and "public" otherwise.
    access_mode: str = ""

    # TLS/SSL (certificate-based encryption -- tgdatabridge/db/tls_config.py).
    # Only paths and on/off switches, on exactly the same principle as the
    # SSH fields above: the client key's own passphrase is never written
    # here, only the paths to the certificate/key files. All optional, so
    # a profile saved by an earlier version loads unchanged.
    tls_enabled: bool = False
    tls_verify_cert: bool = True
    tls_verify_hostname: bool = True
    tls_ca_cert_path: str = ""
    tls_client_cert_path: str = ""
    tls_client_key_path: str = ""

    last_used: str = field(default_factory=lambda: datetime.datetime.now().isoformat(timespec="microseconds"))

    @property
    def key(self) -> tuple:
        """Identity for dedup/upsert purposes -- two profiles with the same
        key are "the same connection", just re-saved (e.g. re-used) again.

        The jump host is part of it: the same database reached directly
        and reached through a bastion are two different connections, and
        collapsing them would silently overwrite one with the other."""
        return (self.engine, self.host, self.port, self.database, self.username,
                self.schema or "", self.ssh_host or "")

    @property
    def display_label(self) -> str:
        schema_part = f" [{self.schema}]" if self.schema else ""
        # A file-based source engine ("Excel/CSV" -- see
        # connector_factory.FILE_SOURCE_ENGINES) has no host, port or
        # username, so `database` holds a file path and the usual
        # "user@host:port/db" rendering would come out as the nonsense
        # "@:0/C:\data\sales.xlsx". Show just the path in that case, which
        # doubles as a recent-files list in the connection dialog.
        if not self.host and not self.port:
            return f"{self.database}{schema_part}"
        via = ""
        if self.ssh_enabled and self.ssh_host:
            via = f" via {self.ssh_host}"
        elif self.access_mode == "vpn":
            via = " over VPN"
        # Plain ASCII, not a padlock glyph -- this string reaches plain-text
        # logs and a legacy Windows console code page the same way
        # version.banner() does (see that function's own ASCII-only test).
        lock = " [TLS]" if self.tls_enabled else ""
        return f"{self.username}@{self.host}:{self.port}/{self.database}{schema_part}{via}{lock}"


def _profiles_path(base_dir: Optional[Path]) -> Path:
    return app_data_dir(base_dir) / "connection_profiles.json"


def load_connection_profiles(engine: Optional[str] = None, base_dir: Optional[Path] = None) -> List[ConnectionProfile]:
    """Every saved profile, most-recently-used first; `engine`, if given,
    filters to just that engine's profiles (e.g. only "PostgreSQL" ones for
    the target connection dialog)."""
    raw = _read_json_list(_profiles_path(base_dir))
    profiles = [ConnectionProfile(**p) for p in raw]
    if engine is not None:
        profiles = [p for p in profiles if p.engine == engine]
    return sorted(profiles, key=lambda p: p.last_used, reverse=True)


def save_connection_profile(
    engine: str, host: str, port: int, database: str, username: str,
    schema: Optional[str] = None, base_dir: Optional[Path] = None,
    ssh: Optional[object] = None, access_mode: str = "",
    tls: Optional[object] = None,
) -> ConnectionProfile:
    """Save (or, if an identical connection was already saved, just refresh
    the recency of) a connection profile. Profiles beyond
    _MAX_PROFILES_PER_ENGINE for this engine are dropped, oldest first --
    this is meant to behave like a most-recently-used list, not an
    unbounded address book."""
    path = _profiles_path(base_dir)
    raw = _read_json_list(path)
    profiles = [ConnectionProfile(**p) for p in raw]

    ssh_fields = {}
    if ssh is not None and getattr(ssh, "enabled", False):
        # Read defensively rather than importing SshTunnelConfig: this
        # module is used by the headless CLI and must not grow a
        # dependency on the db layer just to persist a few strings.
        ssh_fields = {
            "ssh_enabled": True,
            "ssh_host": getattr(ssh, "host", "") or "",
            "ssh_port": int(getattr(ssh, "port", 22) or 22),
            "ssh_username": getattr(ssh, "username", "") or "",
            "ssh_auth_method": getattr(ssh, "auth_method", "") or "",
            "ssh_private_key_path": getattr(ssh, "private_key_path", "") or "",
            "ssh_remote_host": getattr(ssh, "remote_host", "") or "",
            "ssh_remote_port": int(getattr(ssh, "remote_port", 0) or 0),
            "ssh_verify_host_key": bool(getattr(ssh, "verify_host_key", False)),
        }

    tls_fields = {}
    if tls is not None and getattr(tls, "enabled", False):
        # Read defensively rather than importing TlsConfig -- same
        # reasoning as ssh_fields above. Never the client key's own
        # passphrase, only the paths, matching TlsConfig's own field split.
        tls_fields = {
            "tls_enabled": True,
            "tls_verify_cert": bool(getattr(tls, "verify_cert", True)),
            "tls_verify_hostname": bool(getattr(tls, "verify_hostname", True)),
            "tls_ca_cert_path": getattr(tls, "ca_cert_path", "") or "",
            "tls_client_cert_path": getattr(tls, "client_cert_path", "") or "",
            "tls_client_key_path": getattr(tls, "client_key_path", "") or "",
        }

    new_profile = ConnectionProfile(
        engine=engine, host=host, port=port, database=database,
        username=username, schema=schema, access_mode=access_mode,
        **ssh_fields, **tls_fields,
    )
    profiles = [p for p in profiles if p.key != new_profile.key]
    profiles.append(new_profile)

    # enforce the per-engine cap, oldest-first eviction
    by_engine: dict = {}
    for p in sorted(profiles, key=lambda p: p.last_used):
        by_engine.setdefault(p.engine, []).append(p)
    kept: List[ConnectionProfile] = []
    for engine_profiles in by_engine.values():
        kept.extend(engine_profiles[-_MAX_PROFILES_PER_ENGINE:])

    _write_json_list(path, [asdict(p) for p in kept])
    return new_profile


def delete_connection_profile(profile: ConnectionProfile, base_dir: Optional[Path] = None) -> None:
    path = _profiles_path(base_dir)
    raw = _read_json_list(path)
    profiles = [ConnectionProfile(**p) for p in raw]
    profiles = [p for p in profiles if p.key != profile.key]
    _write_json_list(path, [asdict(p) for p in profiles])


# --------------------------------------------------------- conversion history


@dataclass
class ConversionRunRecord:
    id: str
    timestamp: str
    schema_name: str
    source_engine: str
    source_database: str
    target_engine: str
    target_database: str
    target_schema: Optional[str]
    total_objects: int
    automatic_pct: float
    estimated_manual_hours: float
    action_item_count: int
    report_path: Optional[str] = None
    ddl_path: Optional[str] = None
    # OS/AD username that ran this conversion (ENTERPRISE_READINESS.md
    # section 3, item 2: "Capture who ran what"). Defaults to "" (not
    # auto-captured here) so ConversionRunRecord(**r) round-trips old
    # conversion_history.json records written before this field existed --
    # they simply come back with an empty actor instead of failing to load.
    actor: str = ""

    @property
    def display_label(self) -> str:
        who = f"  ·  {self.actor}" if self.actor else ""
        return (
            f"{self.timestamp}  {self.source_engine} -> {self.target_engine}  "
            f"({self.schema_name}, {self.automatic_pct}% automatic){who}"
        )


def _history_path(base_dir: Optional[Path]) -> Path:
    return app_data_dir(base_dir) / "conversion_history.json"


def load_conversion_history(base_dir: Optional[Path] = None) -> List[ConversionRunRecord]:
    """Every recorded run, most recent first."""
    raw = _read_json_list(_history_path(base_dir))
    records = [ConversionRunRecord(**r) for r in raw]
    return sorted(records, key=lambda r: r.timestamp, reverse=True)


def save_run_artifacts(
    run_id: str, report_html: Optional[str], ddl_text: Optional[str], base_dir: Optional[Path] = None,
) -> tuple:
    """Write an auto-saved copy of this run's report/DDL under
    history_files/ so the History dialog can reopen it later even if the
    user never explicitly clicked "Save Report.../Save DDL...". Returns
    (report_path, ddl_path) as strings, or None for whichever wasn't given
    (an empty string is treated the same as not given -- nothing worth
    saving)."""
    files_dir = app_data_dir(base_dir) / "history_files"
    files_dir.mkdir(parents=True, exist_ok=True)
    # run_id is generated by record_conversion_run and is already
    # filesystem-safe (see there), but this is re-asserted here since
    # save_run_artifacts is a public function callers could invoke directly.
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", run_id)

    report_path = None
    if report_html and report_html.strip():
        p = files_dir / f"{safe_id}_report.html"
        p.write_text(report_html, encoding="utf-8")
        report_path = str(p)

    ddl_path = None
    if ddl_text and ddl_text.strip():
        p = files_dir / f"{safe_id}_ddl.sql"
        p.write_text(ddl_text, encoding="utf-8")
        ddl_path = str(p)

    return report_path, ddl_path


def record_conversion_run(
    schema_name: str, source_engine: str, source_database: str,
    target_engine: str, target_database: str, target_schema: Optional[str],
    total_objects: int, automatic_pct: float, estimated_manual_hours: float,
    action_item_count: int,
    report_html: Optional[str] = None, ddl_text: Optional[str] = None,
    actor: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> ConversionRunRecord:
    """Append a new record to conversion_history.json (optionally
    auto-saving `report_html`/`ddl_text` alongside it via
    save_run_artifacts), capped to the _MAX_HISTORY_RECORDS most recent
    runs -- older records (and their history_files/ artifacts) are dropped
    rather than growing this file forever.

    `actor`, if not given, is auto-captured via
    tgdatabridge.utils.logger.current_actor() (the OS/AD username) -- callers
    only need to pass it explicitly in tests wanting a deterministic
    value."""
    timestamp = datetime.datetime.now().isoformat(timespec="microseconds")
    run_id = f"{timestamp.replace(':', '').replace('-', '')}_{uuid.uuid4().hex[:8]}"

    report_path, ddl_path = save_run_artifacts(run_id, report_html, ddl_text, base_dir)

    record = ConversionRunRecord(
        id=run_id, timestamp=timestamp, schema_name=schema_name,
        source_engine=source_engine, source_database=source_database,
        target_engine=target_engine, target_database=target_database,
        target_schema=target_schema, total_objects=total_objects,
        automatic_pct=automatic_pct, estimated_manual_hours=estimated_manual_hours,
        action_item_count=action_item_count, report_path=report_path, ddl_path=ddl_path,
        actor=actor if actor is not None else current_actor(),
    )

    path = _history_path(base_dir)
    raw = _read_json_list(path)
    records = [ConversionRunRecord(**r) for r in raw]
    records.append(record)
    records = sorted(records, key=lambda r: r.timestamp, reverse=True)

    kept, dropped = records[:_MAX_HISTORY_RECORDS], records[_MAX_HISTORY_RECORDS:]
    for old in dropped:
        # clean up that run's auto-saved artifacts too, so history_files/
        # doesn't grow forever alongside a capped JSON index.
        for p in (old.report_path, old.ddl_path):
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass

    _write_json_list(path, [asdict(r) for r in kept])
    return record


# ------------------------------------------------------ migration checkpoints


@dataclass
class ShardCheckpoint:
    """Resumable progress for one shard of one table -- see
    tgdatabridge.core.sharding. A large table split across several workers needs
    progress tracked per slice, not per table: without this, one failed
    shard eight hours into a run would force every *other* shard of that
    table to be re-copied too, since the table as a whole would still be
    "in_progress"."""
    status: str = "pending"  # "pending" | "in_progress" | "done" | "failed"
    rows_copied: int = 0
    batches_completed: int = 0
    checksum: int = 0


@dataclass
class TableCheckpoint:
    """Resumable progress for one table within one MigrationCheckpoint.
    `batches_completed` is a count of whole batches already written to the
    target -- not a row offset -- because that's the unit migrate_table
    actually writes and can safely skip re-writing; see
    tgdatabridge.core.migrator.migrate_table's own checkpoint-handling comment for
    why re-reading a skipped batch from the source (unavoidable -- there is
    no portable SQL OFFSET plumbed through this tool's fetch_batches) is an
    acceptable cost but re-inserting it into the target is not.

    `shards` is populated only when the table was actually split for
    parallel migration (tgdatabridge.core.sharding.plan_shards returned more than
    one shard). An unsharded table leaves it empty and uses the
    table-level `status`/`rows_copied`/`batches_completed` fields exactly
    as before sharding existed, so old checkpoint files keep working and
    the unsharded path is unchanged."""
    status: str = "pending"  # "pending" | "in_progress" | "done" | "failed"
    rows_copied: int = 0
    batches_completed: int = 0
    shards: Dict[str, ShardCheckpoint] = field(default_factory=dict)


@dataclass
class MigrationCheckpoint:
    """Resumable progress for one migrate_schema() run, keyed by
    `checkpoint_id` (see checkpoint_id_for) so the same logical migration
    (same source database, target database, schema, and target engine)
    reuses the same on-disk checkpoint file across separate app runs -- a
    table already marked "done" here is skipped entirely the next time
    migrate_schema() runs against it, instead of re-copying rows that
    already landed on the target."""
    checkpoint_id: str
    schema_name: str
    source_engine: str
    target_engine: str
    tables: Dict[str, TableCheckpoint] = field(default_factory=dict)
    updated: str = field(default_factory=lambda: datetime.datetime.now().isoformat(timespec="microseconds"))
    # Which algorithm produced the per-shard checksums stored in `tables`
    # (tgdatabridge.core.validation.CHECKSUM_ALGORITHM). A resumed run reuses a
    # finished shard's stored checksum when combining shards, so mixing
    # two algorithms' values would report a data-integrity failure that
    # isn't real. load_checkpoint compares this and, on a mismatch, drops
    # the stored checksums and sets `checksums_stale`.
    checksum_algorithm: str = ""
    # Transient (never written to disk): set by load_checkpoint when the
    # stored checksums came from a different algorithm and were therefore
    # discarded. migrate_schema reads it to skip the checksum half of
    # validation for an affected table -- row counts are still compared.
    checksums_stale: bool = field(default=False, compare=False, repr=False)

    @property
    def all_done(self) -> bool:
        return bool(self.tables) and all(t.status == "done" for t in self.tables.values())

    @property
    def has_failures(self) -> bool:
        return any(t.status == "failed" for t in self.tables.values())


def checkpoint_id_for(source_database: str, target_database: str, schema_name: str, target_engine: str) -> str:
    """A stable, filesystem-safe identifier for "this logical migration" --
    the same four inputs always produce the same id, so re-running
    migrate_schema against the same source/target/schema/engine
    combination finds and resumes the same checkpoint file rather than
    starting a fresh one every time. Deliberately NOT random (unlike
    conversion history's run_id, which identifies one specific historical
    run) -- a checkpoint's whole purpose is to be found again by a
    *different*, later run of what is logically the same migration."""
    raw = f"{source_database}|{target_database}|{schema_name}|{target_engine}".lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _checkpoints_dir(base_dir: Optional[Path]) -> Path:
    directory = app_data_dir(base_dir) / _CHECKPOINTS_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _checkpoint_path(checkpoint_id: str, base_dir: Optional[Path]) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", checkpoint_id)
    return _checkpoints_dir(base_dir) / f"{safe_id}.json"


def load_checkpoint(checkpoint_id: str, base_dir: Optional[Path] = None) -> Optional[MigrationCheckpoint]:
    """None if no checkpoint has ever been saved for this id, or if the
    file on disk is missing/corrupted -- callers should treat that exactly
    like "start this migration from scratch", the same tolerance
    _read_json_list already gives every other file in this module."""
    path = _checkpoint_path(checkpoint_id, base_dir)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    try:
        tables = {}
        for name, raw_table in data.get("tables", {}).items():
            # `shards` needs rebuilding into real ShardCheckpoint objects
            # rather than being passed through as raw dicts. A file written
            # before sharding existed simply has no "shards" key and gets
            # the empty default, which is what an unsharded table uses.
            raw_shards = raw_table.pop("shards", None) or {}
            table_checkpoint = TableCheckpoint(**raw_table)
            table_checkpoint.shards = {
                shard_key: ShardCheckpoint(**raw_shard) for shard_key, raw_shard in raw_shards.items()
            }
            tables[name] = table_checkpoint
        from tgdatabridge.core.validation import CHECKSUM_ALGORITHM

        stored_algorithm = data.get("checksum_algorithm", "")
        checkpoint = MigrationCheckpoint(
            checkpoint_id=data["checkpoint_id"],
            schema_name=data["schema_name"],
            source_engine=data["source_engine"],
            target_engine=data["target_engine"],
            tables=tables,
            updated=data.get("updated", ""),
            checksum_algorithm=stored_algorithm or CHECKSUM_ALGORITHM,
        )
        if stored_algorithm and stored_algorithm != CHECKSUM_ALGORITHM:
            # Written by a build using a different row_checksum. The
            # *progress* is still perfectly good -- which batches landed on
            # the target has nothing to do with how they were hashed -- so
            # the checkpoint is kept and only the checksums are dropped.
            # Re-copying rows to avoid a stale hash would be a far worse
            # trade than skipping one half of one check.
            checkpoint.checksums_stale = True
            checkpoint.checksum_algorithm = CHECKSUM_ALGORITHM
            # Only shards carry a checksum -- TableCheckpoint has none, which
            # is why migrator reads it with getattr(..., 0). An unsharded
            # table resumed as "done" therefore contributes no checksum at
            # all and its validation is skipped for want of column names,
            # so there is nothing to invalidate there.
            for table_checkpoint in checkpoint.tables.values():
                for shard_checkpoint in table_checkpoint.shards.values():
                    shard_checkpoint.checksum = 0
        return checkpoint
    except (KeyError, TypeError):
        return None


def _current_checksum_algorithm() -> str:
    """Imported lazily: app_storage is imported by tgdatabridge.core.validation's
    own callers, and a module-level import the other way round would be a
    cycle."""
    from tgdatabridge.core.validation import CHECKSUM_ALGORITHM
    return CHECKSUM_ALGORITHM


def save_checkpoint(checkpoint: MigrationCheckpoint, base_dir: Optional[Path] = None) -> None:
    """Overwrites the on-disk checkpoint for checkpoint.checkpoint_id with
    its current in-memory state -- intended to be called frequently (after
    every batch/table migrate_schema processes, via its
    on_checkpoint_update callback), not just once at the end, so a crash
    mid-run leaves accurate, resumable progress behind rather than none."""
    checkpoint.updated = datetime.datetime.now().isoformat(timespec="microseconds")
    path = _checkpoint_path(checkpoint.checkpoint_id, base_dir)
    data = {
        "checkpoint_id": checkpoint.checkpoint_id,
        "schema_name": checkpoint.schema_name,
        "source_engine": checkpoint.source_engine,
        "target_engine": checkpoint.target_engine,
        "updated": checkpoint.updated,
        # Recorded so a later build using a different row_checksum can tell
        # that these checksums aren't comparable with its own -- see
        # load_checkpoint. `checksums_stale` is deliberately not written:
        # it describes this load, not the file.
        "checksum_algorithm": checkpoint.checksum_algorithm or _current_checksum_algorithm(),
        "tables": {name: asdict(t) for name, t in checkpoint.tables.items()},
    }
    # Written to a temporary file and renamed into place, rather than
    # opened over the top of the existing one. This file exists precisely
    # to survive a crash, and rewriting it in place means a crash *during*
    # the rewrite leaves a truncated, unparseable checkpoint -- destroying
    # the progress record at exactly the moment it's needed. os.replace is
    # atomic on both Windows and POSIX, so a reader afterwards sees either
    # the complete old file or the complete new one, never a partial mix.
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(temp_path, path)


DEFAULT_CHECKPOINT_FLUSH_SECONDS = 5.0


class CheckpointWriter:
    """Persists a MigrationCheckpoint, coalescing frequent progress
    updates into at most one disk write per `min_interval_seconds`.

    Why this exists (SCALE.md section 1.5): migrate_schema calls its
    checkpoint callback after **every batch**, and save_checkpoint
    rewrites the entire checkpoint file each time. At the throughput the
    COPY path and intra-table sharding now reach, that's tens of full-file
    rewrites per second per shard, all serialized behind one lock -- the
    checkpoint write becomes a contention point on the very thing it's
    supposed to be quietly recording.

    Two methods, deliberately distinct:

      - `request()` -- "progress advanced." Writes only if enough time has
        passed since the last write. Safe to call after every batch.
      - `flush()` -- "this state must not be lost." Always writes.
        Used for terminal transitions (a table or shard reaching done or
        failed), which are exactly the records a resume depends on.

    **The trade-off, stated plainly.** Between two writes the on-disk
    checkpoint under-reports progress. If the process dies in that window,
    a resumed run re-copies the batches written since the last flush, and
    since inserts aren't idempotent that means duplicate rows on the
    target. This is a *widening* of a window that already existed (the
    old per-batch flush still had a one-batch gap between the insert and
    the checkpoint write), not a new failure mode -- but it is wider, and
    that's the deal being struck: `min_interval_seconds` is exactly how
    much re-work a crash can cost. Set it to 0 to write on every request,
    which is byte-for-byte the pre-throttling behaviour.

    Two things make the wider window tolerable rather than alarming:
    post-migration validation (row count + checksum, see
    tgdatabridge.core.validation) *detects* duplicates rather than letting them
    pass silently, and with deferred constraints (SCALE.md section 1.3)
    the primary key is created after the load, where a duplicate fails
    loudly instead of being absorbed.

    Thread-safe: sharded migrations call this from several worker threads
    at once, so the interval check and the write happen under one lock --
    without it, N threads could all decide to write simultaneously and
    defeat the throttling entirely.

    `save_fn` and `time_fn` are injectable so tests can exercise the
    coalescing logic without touching a disk or sleeping.
    """

    def __init__(
        self,
        checkpoint: "MigrationCheckpoint",
        base_dir: Optional[Path] = None,
        min_interval_seconds: float = DEFAULT_CHECKPOINT_FLUSH_SECONDS,
        save_fn=None,
        time_fn=None,
    ):
        self.checkpoint = checkpoint
        self.base_dir = base_dir
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._save_fn = save_fn if save_fn is not None else save_checkpoint
        self._time_fn = time_fn if time_fn is not None else time.monotonic
        self._lock = threading.Lock()
        # None rather than 0.0 so the very first request() always writes:
        # an early "in_progress" marker on disk is what makes a crash in
        # the first few seconds of a long run resumable at all.
        self._last_write: Optional[float] = None
        self.writes = 0
        self.skipped = 0

    def request(self) -> bool:
        """Persist if the throttle interval has elapsed. Returns True if a
        write actually happened, for tests and diagnostics."""
        with self._lock:
            now = self._time_fn()
            if (self._last_write is not None
                    and self.min_interval_seconds > 0
                    and (now - self._last_write) < self.min_interval_seconds):
                self.skipped += 1
                return False
            return self._write_locked(now)

    def flush(self) -> bool:
        """Persist unconditionally. Use for state whose loss would cost a
        resume real work -- a table or shard reaching done or failed."""
        with self._lock:
            return self._write_locked(self._time_fn())

    def _write_locked(self, now: float) -> bool:
        try:
            self._save_fn(self.checkpoint, base_dir=self.base_dir)
        except OSError:
            # Persisting progress is a resilience feature; failing to
            # write it must never take down the migration that's
            # succeeding. The next request()/flush() tries again.
            return False
        self._last_write = now
        self.writes += 1
        return True


def delete_checkpoint(checkpoint_id: str, base_dir: Optional[Path] = None) -> None:
    """Called once a migration finishes with nothing failed -- there's
    nothing left to ever resume, so the checkpoint file is removed instead
    of accumulating forever. Silently a no-op if there was nothing to
    delete (a dry-run-only migration, or one that never used a checkpoint
    at all)."""
    path = _checkpoint_path(checkpoint_id, base_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------- sync watermarks

_WATERMARKS_DIR_NAME = "watermarks"


def _watermarks_dir(base_dir: Optional[Path]) -> Path:
    directory = app_data_dir(base_dir) / _WATERMARKS_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _watermarks_path(sync_id: str, base_dir: Optional[Path]) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", sync_id)
    return _watermarks_dir(base_dir) / f"{safe_id}.json"


def load_watermarks(sync_id: str, base_dir: Optional[Path] = None) -> Dict[str, str]:
    """The per-table high-water marks the last incremental sync reached.

    Keyed by the same `checkpoint_id_for` identity a full migration uses,
    so the marks belong to one logical source->target->schema pairing and
    are found again by a later run of the app rather than restarting from
    "read everything".

    An unreadable or missing file returns {} -- which makes the next sync
    read every row and compare, i.e. correct but slow. That is the right
    way to fail: the alternative, inventing a mark, silently skips rows.
    """
    path = _watermarks_path(sync_id, base_dir)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    marks = data.get("tables", {})
    return {str(k): str(v) for k, v in marks.items()} if isinstance(marks, dict) else {}


def save_watermarks(sync_id: str, marks: Dict[str, str],
                    base_dir: Optional[Path] = None) -> None:
    path = _watermarks_path(sync_id, base_dir)
    payload = {
        "sync_id": sync_id,
        "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tables": dict(marks),
    }
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    tmp.replace(path)


def delete_watermarks(sync_id: str, base_dir: Optional[Path] = None) -> None:
    """Forget the marks, so the next sync compares everything again."""
    path = _watermarks_path(sync_id, base_dir)
    if path.exists():
        path.unlink()
