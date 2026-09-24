"""Tests for tgdatabridge.utils.app_storage -- connection profile and conversion
history persistence. Every call uses an explicit `base_dir` (a throwaway
temp directory, cleaned up at the end of each test) so nothing here ever
touches the real user's AppData/home directory. Plain tempfile is used
instead of a pytest fixture so these tests run the same way whether
invoked via `pytest` or this repo's fixture-free hand-rolled test runner."""
import json
import pathlib
import shutil
import tempfile
import time

from tgdatabridge.utils import app_storage as st


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="tgdatabridge_test_"))


def test_app_data_dir_creates_directory():
    base = _tmp_dir()
    try:
        d = base / "nested" / "dir"
        result = st.app_data_dir(base_dir=d)
        assert result == d
        assert d.is_dir()
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ------------------------------------------------ shared storage (section 5)
# ENTERPRISE_READINESS.md section 5, item 4: "Multi-user/shared deployment".
# These tests monkeypatch st.local_app_data_dir (restored in `finally`)
# instead of calling app_data_dir(base_dir=None) directly, since the real
# no-argument call would otherwise touch this machine's actual home/AppData
# directory.


def test_app_data_dir_uses_local_dir_when_nothing_configured():
    local = _tmp_dir()
    original = st.local_app_data_dir
    try:
        st.local_app_data_dir = lambda base_dir=None: local
        assert st.app_data_dir() == local
    finally:
        st.local_app_data_dir = original
        shutil.rmtree(local, ignore_errors=True)


def test_app_data_dir_redirects_to_configured_shared_path():
    local = _tmp_dir()
    shared = _tmp_dir()
    original = st.local_app_data_dir
    try:
        st.local_app_data_dir = lambda base_dir=None: local
        (local / "settings.json").write_text(
            json.dumps({"shared_storage_path": str(shared)}), encoding="utf-8")
        assert st.app_data_dir() == shared
    finally:
        st.local_app_data_dir = original
        shutil.rmtree(local, ignore_errors=True)
        shutil.rmtree(shared, ignore_errors=True)


def test_app_data_dir_falls_back_to_local_when_settings_missing():
    local = _tmp_dir()
    original = st.local_app_data_dir
    try:
        st.local_app_data_dir = lambda base_dir=None: local
        # no settings.json written at all
        assert st.app_data_dir() == local
    finally:
        st.local_app_data_dir = original
        shutil.rmtree(local, ignore_errors=True)


def test_app_data_dir_falls_back_to_local_when_settings_corrupted():
    local = _tmp_dir()
    original = st.local_app_data_dir
    try:
        st.local_app_data_dir = lambda base_dir=None: local
        (local / "settings.json").write_text("{not valid json", encoding="utf-8")
        assert st.app_data_dir() == local
    finally:
        st.local_app_data_dir = original
        shutil.rmtree(local, ignore_errors=True)


def test_app_data_dir_falls_back_to_local_when_shared_path_blank():
    local = _tmp_dir()
    original = st.local_app_data_dir
    try:
        st.local_app_data_dir = lambda base_dir=None: local
        (local / "settings.json").write_text(
            json.dumps({"shared_storage_path": "   "}), encoding="utf-8")
        assert st.app_data_dir() == local
    finally:
        st.local_app_data_dir = original
        shutil.rmtree(local, ignore_errors=True)


def test_explicit_base_dir_always_overrides_shared_config():
    local = _tmp_dir()
    shared = _tmp_dir()
    explicit = _tmp_dir()
    original = st.local_app_data_dir
    try:
        st.local_app_data_dir = lambda base_dir=None: local
        (local / "settings.json").write_text(
            json.dumps({"shared_storage_path": str(shared)}), encoding="utf-8")
        # base_dir, the test-injection seam, always wins over any
        # configured shared path -- matches every other function in this
        # module treating base_dir as an unconditional override.
        assert st.app_data_dir(base_dir=explicit) == explicit
    finally:
        st.local_app_data_dir = original
        shutil.rmtree(local, ignore_errors=True)
        shutil.rmtree(shared, ignore_errors=True)
        shutil.rmtree(explicit, ignore_errors=True)


def test_connection_profiles_redirect_to_shared_path():
    local = _tmp_dir()
    shared = _tmp_dir()
    original = st.local_app_data_dir
    try:
        st.local_app_data_dir = lambda base_dir=None: local
        (local / "settings.json").write_text(
            json.dumps({"shared_storage_path": str(shared)}), encoding="utf-8")

        st.save_connection_profile("Oracle", "host1", 1521, "orcl", "scott", "hr")
        assert (shared / "connection_profiles.json").exists()
        assert not (local / "connection_profiles.json").exists()
        profiles = st.load_connection_profiles()
        assert len(profiles) == 1 and profiles[0].host == "host1"
    finally:
        st.local_app_data_dir = original
        shutil.rmtree(local, ignore_errors=True)
        shutil.rmtree(shared, ignore_errors=True)


# ------------------------------------------------------- connection profiles


def test_save_and_load_connection_profile_roundtrip():
    base = _tmp_dir()
    try:
        st.save_connection_profile("Oracle", "host1", 1521, "orcl", "scott", "hr", base_dir=base)
        profiles = st.load_connection_profiles(base_dir=base)
        assert len(profiles) == 1
        p = profiles[0]
        assert p.engine == "Oracle"
        assert p.host == "host1"
        assert p.port == 1521
        assert p.database == "orcl"
        assert p.username == "scott"
        assert p.schema == "hr"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_saved_profile_never_contains_a_password_field():
    base = _tmp_dir()
    try:
        st.save_connection_profile("PostgreSQL", "host1", 5432, "app", "admin", "public", base_dir=base)
        raw = json.loads((base / "connection_profiles.json").read_text(encoding="utf-8"))
        assert "password" not in raw[0]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_saving_same_connection_again_upserts_not_duplicates():
    base = _tmp_dir()
    try:
        st.save_connection_profile("MySQL", "h", 3306, "db", "u", None, base_dir=base)
        st.save_connection_profile("MySQL", "h", 3306, "db", "u", None, base_dir=base)
        profiles = st.load_connection_profiles(base_dir=base)
        assert len(profiles) == 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_different_schema_is_a_different_profile():
    base = _tmp_dir()
    try:
        st.save_connection_profile("SQL Server", "h", 1433, "db", "u", "dbo", base_dir=base)
        st.save_connection_profile("SQL Server", "h", 1433, "db", "u", "sales", base_dir=base)
        profiles = st.load_connection_profiles(base_dir=base)
        assert len(profiles) == 2
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_connection_profiles_filters_by_engine():
    base = _tmp_dir()
    try:
        st.save_connection_profile("Oracle", "h", 1521, "db1", "u", None, base_dir=base)
        st.save_connection_profile("DB2", "h", 50000, "db2", "u", None, base_dir=base)
        oracle_only = st.load_connection_profiles(engine="Oracle", base_dir=base)
        assert len(oracle_only) == 1
        assert oracle_only[0].engine == "Oracle"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_connection_profiles_most_recent_first():
    base = _tmp_dir()
    try:
        st.save_connection_profile("Oracle", "first", 1521, "db", "u", None, base_dir=base)
        time.sleep(0.01)
        st.save_connection_profile("Oracle", "second", 1521, "db", "u", None, base_dir=base)
        profiles = st.load_connection_profiles(base_dir=base)
        assert profiles[0].host == "second"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_profile_cap_evicts_oldest_per_engine():
    base = _tmp_dir()
    original_cap = st._MAX_PROFILES_PER_ENGINE
    st._MAX_PROFILES_PER_ENGINE = 3
    try:
        for i in range(5):
            st.save_connection_profile("Oracle", f"host{i}", 1521, "db", "u", None, base_dir=base)
            time.sleep(0.001)
        profiles = st.load_connection_profiles(base_dir=base)
        assert len(profiles) == 3
        hosts = {p.host for p in profiles}
        assert hosts == {"host2", "host3", "host4"}
    finally:
        st._MAX_PROFILES_PER_ENGINE = original_cap
        shutil.rmtree(base, ignore_errors=True)


def test_delete_connection_profile():
    base = _tmp_dir()
    try:
        st.save_connection_profile("Oracle", "h", 1521, "db", "u", None, base_dir=base)
        profile = st.load_connection_profiles(base_dir=base)[0]
        st.delete_connection_profile(profile, base_dir=base)
        assert st.load_connection_profiles(base_dir=base) == []
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_display_label_includes_schema_when_present():
    p = st.ConnectionProfile(engine="Oracle", host="h", port=1521, database="orcl", username="scott", schema="hr")
    assert p.display_label == "scott@h:1521/orcl [hr]"


def test_display_label_omits_schema_when_absent():
    p = st.ConnectionProfile(engine="MySQL", host="h", port=3306, database="app", username="root", schema=None)
    assert p.display_label == "root@h:3306/app"


def test_load_connection_profiles_survives_corrupted_file():
    base = _tmp_dir()
    try:
        (base / "connection_profiles.json").write_text("{not valid json", encoding="utf-8")
        assert st.load_connection_profiles(base_dir=base) == []
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------- conversion history


def test_record_and_load_conversion_run_roundtrip():
    base = _tmp_dir()
    try:
        record = st.record_conversion_run(
            schema_name="HR", source_engine="Oracle", source_database="orcl",
            target_engine="DB2", target_database="appdb", target_schema="APP",
            total_objects=10, automatic_pct=90.0, estimated_manual_hours=1.5,
            action_item_count=2, base_dir=base,
        )
        history = st.load_conversion_history(base_dir=base)
        assert len(history) == 1
        assert history[0].id == record.id
        assert history[0].target_engine == "DB2"
        assert history[0].automatic_pct == 90.0
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_conversion_run_captures_explicit_actor():
    # ENTERPRISE_READINESS.md section 3, item 2: "Capture who ran what".
    base = _tmp_dir()
    try:
        record = st.record_conversion_run(
            schema_name="HR", source_engine="Oracle", source_database="orcl",
            target_engine="PostgreSQL", target_database="appdb", target_schema="public",
            total_objects=3, automatic_pct=100.0, estimated_manual_hours=0.0,
            action_item_count=0, actor="carol", base_dir=base,
        )
        assert record.actor == "carol"
        history = st.load_conversion_history(base_dir=base)
        assert history[0].actor == "carol"
        assert "carol" in history[0].display_label
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_conversion_run_auto_captures_actor_when_not_given():
    base = _tmp_dir()
    try:
        record = st.record_conversion_run(
            schema_name="HR", source_engine="Oracle", source_database="orcl",
            target_engine="PostgreSQL", target_database="appdb", target_schema="public",
            total_objects=1, automatic_pct=100.0, estimated_manual_hours=0.0,
            action_item_count=0, base_dir=base,
        )
        assert record.actor  # never blank -- falls back to current_actor()'s "unknown"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_conversion_run_record_without_actor_field_loads_with_empty_default():
    # Backward compatibility: a conversion_history.json written before the
    # `actor` field existed must still load without crashing.
    base = _tmp_dir()
    try:
        path = base / "conversion_history.json"
        base.mkdir(parents=True, exist_ok=True)
        old_record = {
            "id": "old_run_1", "timestamp": "2025-01-01T00:00:00",
            "schema_name": "HR", "source_engine": "Oracle", "source_database": "orcl",
            "target_engine": "PostgreSQL", "target_database": "appdb", "target_schema": "public",
            "total_objects": 5, "automatic_pct": 80.0, "estimated_manual_hours": 1.0,
            "action_item_count": 1,
            # deliberately no "actor" key, and no report_path/ddl_path either
        }
        path.write_text(json.dumps([old_record]), encoding="utf-8")

        history = st.load_conversion_history(base_dir=base)
        assert len(history) == 1
        assert history[0].actor == ""
        assert history[0].id == "old_run_1"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_conversion_run_saves_artifacts_when_given():
    base = _tmp_dir()
    try:
        record = st.record_conversion_run(
            schema_name="HR", source_engine="Oracle", source_database="orcl",
            target_engine="PostgreSQL", target_database="appdb", target_schema="public",
            total_objects=5, automatic_pct=100.0, estimated_manual_hours=0.0,
            action_item_count=0, report_html="<html>report</html>", ddl_text="CREATE TABLE t (x INT);",
            base_dir=base,
        )
        assert record.report_path and open(record.report_path, encoding="utf-8").read() == "<html>report</html>"
        assert record.ddl_path and open(record.ddl_path, encoding="utf-8").read() == "CREATE TABLE t (x INT);"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_conversion_run_skips_artifacts_when_not_given():
    base = _tmp_dir()
    try:
        record = st.record_conversion_run(
            schema_name="HR", source_engine="Oracle", source_database="orcl",
            target_engine="MySQL", target_database="appdb", target_schema=None,
            total_objects=1, automatic_pct=100.0, estimated_manual_hours=0.0,
            action_item_count=0, base_dir=base,
        )
        assert record.report_path is None
        assert record.ddl_path is None
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_history_most_recent_first():
    base = _tmp_dir()
    try:
        st.record_conversion_run(
            schema_name="A", source_engine="Oracle", source_database="db", target_engine="MySQL",
            target_database="db", target_schema=None, total_objects=1, automatic_pct=100.0,
            estimated_manual_hours=0.0, action_item_count=0, base_dir=base,
        )
        time.sleep(0.01)
        st.record_conversion_run(
            schema_name="B", source_engine="Oracle", source_database="db", target_engine="MySQL",
            target_database="db", target_schema=None, total_objects=1, automatic_pct=100.0,
            estimated_manual_hours=0.0, action_item_count=0, base_dir=base,
        )
        history = st.load_conversion_history(base_dir=base)
        assert history[0].schema_name == "B"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_history_cap_evicts_oldest_and_their_artifacts():
    base = _tmp_dir()
    original_cap = st._MAX_HISTORY_RECORDS
    st._MAX_HISTORY_RECORDS = 2
    try:
        first = st.record_conversion_run(
            schema_name="A", source_engine="Oracle", source_database="db", target_engine="MySQL",
            target_database="db", target_schema=None, total_objects=1, automatic_pct=100.0,
            estimated_manual_hours=0.0, action_item_count=0, report_html="<a/>", base_dir=base,
        )
        time.sleep(0.01)
        st.record_conversion_run(
            schema_name="B", source_engine="Oracle", source_database="db", target_engine="MySQL",
            target_database="db", target_schema=None, total_objects=1, automatic_pct=100.0,
            estimated_manual_hours=0.0, action_item_count=0, base_dir=base,
        )
        time.sleep(0.01)
        st.record_conversion_run(
            schema_name="C", source_engine="Oracle", source_database="db", target_engine="MySQL",
            target_database="db", target_schema=None, total_objects=1, automatic_pct=100.0,
            estimated_manual_hours=0.0, action_item_count=0, base_dir=base,
        )
        history = st.load_conversion_history(base_dir=base)
        assert len(history) == 2
        assert {r.schema_name for r in history} == {"B", "C"}
        # the evicted run's report file should have been cleaned up too
        assert not pathlib.Path(first.report_path).exists()
    finally:
        st._MAX_HISTORY_RECORDS = original_cap
        shutil.rmtree(base, ignore_errors=True)


def test_display_label_format():
    r = st.ConversionRunRecord(
        id="x", timestamp="2026-07-31T12:00:00", schema_name="HR",
        source_engine="Oracle", source_database="orcl", target_engine="DB2",
        target_database="appdb", target_schema="APP", total_objects=10,
        automatic_pct=90.0, estimated_manual_hours=1.5, action_item_count=2,
    )
    assert r.display_label == "2026-07-31T12:00:00  Oracle -> DB2  (HR, 90.0% automatic)"


# ------------------------------------------------------ migration checkpoints


def test_checkpoint_id_for_is_deterministic():
    a = st.checkpoint_id_for("orcl", "appdb", "HR", "PostgreSQL")
    b = st.checkpoint_id_for("orcl", "appdb", "HR", "PostgreSQL")
    assert a == b


def test_checkpoint_id_for_is_case_insensitive():
    a = st.checkpoint_id_for("ORCL", "APPDB", "HR", "PostgreSQL")
    b = st.checkpoint_id_for("orcl", "appdb", "hr", "postgresql")
    assert a == b


def test_checkpoint_id_for_differs_for_different_inputs():
    a = st.checkpoint_id_for("orcl", "appdb", "HR", "PostgreSQL")
    b = st.checkpoint_id_for("orcl", "appdb", "HR", "MySQL")
    assert a != b


def test_checkpoint_id_for_is_filesystem_safe():
    checkpoint_id = st.checkpoint_id_for("orcl", "appdb", "HR", "PostgreSQL")
    assert all(c.isalnum() for c in checkpoint_id)


def test_load_checkpoint_returns_none_when_nothing_saved():
    base = _tmp_dir()
    try:
        assert st.load_checkpoint("nonexistent-id", base_dir=base) is None
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_save_and_load_checkpoint_round_trips():
    base = _tmp_dir()
    try:
        checkpoint = st.MigrationCheckpoint(
            checkpoint_id="run-1", schema_name="HR",
            source_engine="Oracle", target_engine="PostgreSQL",
        )
        checkpoint.tables["ACCOUNT"] = st.TableCheckpoint(status="done", rows_copied=100, batches_completed=5)
        checkpoint.tables["CUSTOMER"] = st.TableCheckpoint(status="in_progress", rows_copied=10, batches_completed=1)
        st.save_checkpoint(checkpoint, base_dir=base)

        loaded = st.load_checkpoint("run-1", base_dir=base)
        assert loaded is not None
        assert loaded.checkpoint_id == "run-1"
        assert loaded.schema_name == "HR"
        assert loaded.source_engine == "Oracle"
        assert loaded.target_engine == "PostgreSQL"
        assert loaded.tables["ACCOUNT"].status == "done"
        assert loaded.tables["ACCOUNT"].rows_copied == 100
        assert loaded.tables["CUSTOMER"].status == "in_progress"
        assert loaded.tables["CUSTOMER"].batches_completed == 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_save_checkpoint_updates_the_updated_timestamp():
    base = _tmp_dir()
    try:
        checkpoint = st.MigrationCheckpoint(
            checkpoint_id="run-1", schema_name="HR", source_engine="Oracle", target_engine="PostgreSQL",
            updated="2000-01-01T00:00:00.000000",
        )
        st.save_checkpoint(checkpoint, base_dir=base)
        assert checkpoint.updated != "2000-01-01T00:00:00.000000"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_checkpoint_returns_none_for_corrupted_file():
    base = _tmp_dir()
    try:
        checkpoints_dir = st.app_data_dir(base_dir=base) / st._CHECKPOINTS_DIR_NAME
        checkpoints_dir.mkdir(parents=True, exist_ok=True)
        (checkpoints_dir / "run-1.json").write_text("{not valid json", encoding="utf-8")
        assert st.load_checkpoint("run-1", base_dir=base) is None
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_delete_checkpoint_removes_the_file():
    base = _tmp_dir()
    try:
        checkpoint = st.MigrationCheckpoint(
            checkpoint_id="run-1", schema_name="HR", source_engine="Oracle", target_engine="PostgreSQL",
        )
        st.save_checkpoint(checkpoint, base_dir=base)
        assert st.load_checkpoint("run-1", base_dir=base) is not None
        st.delete_checkpoint("run-1", base_dir=base)
        assert st.load_checkpoint("run-1", base_dir=base) is None
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_delete_checkpoint_is_a_no_op_when_nothing_to_delete():
    base = _tmp_dir()
    try:
        st.delete_checkpoint("never-existed", base_dir=base)  # must not raise
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_migration_checkpoint_all_done_true_only_when_every_table_is_done():
    checkpoint = st.MigrationCheckpoint(
        checkpoint_id="run-1", schema_name="HR", source_engine="Oracle", target_engine="PostgreSQL",
    )
    checkpoint.tables["A"] = st.TableCheckpoint(status="done")
    checkpoint.tables["B"] = st.TableCheckpoint(status="done")
    assert checkpoint.all_done is True
    checkpoint.tables["C"] = st.TableCheckpoint(status="pending")
    assert checkpoint.all_done is False


def test_migration_checkpoint_all_done_false_when_empty():
    checkpoint = st.MigrationCheckpoint(
        checkpoint_id="run-1", schema_name="HR", source_engine="Oracle", target_engine="PostgreSQL",
    )
    assert checkpoint.all_done is False


def test_migration_checkpoint_has_failures():
    checkpoint = st.MigrationCheckpoint(
        checkpoint_id="run-1", schema_name="HR", source_engine="Oracle", target_engine="PostgreSQL",
    )
    checkpoint.tables["A"] = st.TableCheckpoint(status="done")
    assert checkpoint.has_failures is False
    checkpoint.tables["B"] = st.TableCheckpoint(status="failed")
    assert checkpoint.has_failures is True


# ------------------------------------------- carrying a pre-rebrand install over

def test_an_existing_install_is_adopted_when_the_product_is_renamed(monkeypatch):
    """The product was renamed; the user's saved connections, run history
    and in-flight migration checkpoints were not. Starting the renamed
    build against an empty folder would look exactly like the rebrand had
    wiped them -- and a lost checkpoint is not cosmetic: the next run
    re-copies every table that had already landed."""
    base = _tmp_dir()
    try:
        legacy = base / st._LEGACY_APP_DIR_NAME
        (legacy / "migration_checkpoints").mkdir(parents=True)
        (legacy / "connection_profiles.json").write_text('[{"engine": "Oracle"}]', encoding="utf-8")
        (legacy / "migration_checkpoints" / "abc.json").write_text('{"checkpoint_id": "abc"}', encoding="utf-8")

        monkeypatch.setenv("APPDATA", str(base))
        current = st.local_app_data_dir()

        assert current == base / st._APP_DIR_NAME
        assert json.loads((current / "connection_profiles.json").read_text(encoding="utf-8")) == [{"engine": "Oracle"}]
        assert (current / "migration_checkpoints" / "abc.json").exists()
        # Copied, not moved: an older build installed alongside this one
        # keeps working from its own folder.
        assert (legacy / "connection_profiles.json").exists()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_adoption_happens_once_and_never_overwrites_newer_data(monkeypatch):
    """Re-copying on every launch would clobber whatever the renamed build
    has since written -- including a checkpoint for a migration running
    right now."""
    base = _tmp_dir()
    try:
        legacy = base / st._LEGACY_APP_DIR_NAME
        legacy.mkdir(parents=True)
        (legacy / "connection_profiles.json").write_text("[]", encoding="utf-8")

        monkeypatch.setenv("APPDATA", str(base))
        current = st.local_app_data_dir()
        (current / "connection_profiles.json").write_text('[{"engine": "MySQL"}]', encoding="utf-8")

        st.local_app_data_dir()          # a second launch
        assert json.loads((current / "connection_profiles.json").read_text(encoding="utf-8")) == [{"engine": "MySQL"}]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_fresh_install_with_no_legacy_folder_just_starts_empty(monkeypatch):
    base = _tmp_dir()
    try:
        monkeypatch.setenv("APPDATA", str(base))
        current = st.local_app_data_dir()
        assert current.is_dir()
        assert list(current.iterdir()) == []
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_an_unreadable_legacy_folder_does_not_stop_the_app_starting(monkeypatch):
    """Not inheriting old settings is an inconvenience. Failing to launch
    because of it is not."""
    base = _tmp_dir()
    try:
        legacy = base / st._LEGACY_APP_DIR_NAME
        legacy.mkdir(parents=True)
        monkeypatch.setattr(st.shutil, "copytree",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
        monkeypatch.setenv("APPDATA", str(base))
        assert st.local_app_data_dir().is_dir()
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ------------------------------------------------------ TLS/SSL persistence


class _FakeTls:
    """Read defensively by save_connection_profile (see its own docstring),
    so a plain stand-in exercises that path without importing TlsConfig
    here -- mirrors this file's existing SSH-config tests, if any, in
    spirit with test_ssh_tunnel.py's own _cfg() helper."""

    def __init__(self, **kwargs):
        self.enabled = True
        self.verify_cert = True
        self.verify_hostname = True
        self.ca_cert_path = ""
        self.client_cert_path = ""
        self.client_key_path = ""
        self.client_key_password = "not-persisted"
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_tls_settings_are_saved_and_restored():
    base = _tmp_dir()
    try:
        st.save_connection_profile(
            "PostgreSQL", "h", 5432, "app", "u", "public", base_dir=base,
            tls=_FakeTls(ca_cert_path="/ca.pem", client_cert_path="/c.crt",
                        client_key_path="/c.key", verify_hostname=False),
        )
        profiles = st.load_connection_profiles(base_dir=base)
        assert len(profiles) == 1
        p = profiles[0]
        assert p.tls_enabled is True
        assert p.tls_verify_cert is True
        assert p.tls_verify_hostname is False
        assert p.tls_ca_cert_path == "/ca.pem"
        assert p.tls_client_cert_path == "/c.crt"
        assert p.tls_client_key_path == "/c.key"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_tls_client_key_password_is_never_persisted():
    base = _tmp_dir()
    try:
        st.save_connection_profile(
            "MySQL", "h", 3306, "db", "u", None, base_dir=base,
            tls=_FakeTls(client_key_password="super-secret"),
        )
        raw = json.loads((base / "connection_profiles.json").read_text(encoding="utf-8"))
        assert "super-secret" not in json.dumps(raw)
        assert not any("password" in k for k in raw[0] if k.startswith("tls"))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_disabled_tls_object_is_not_saved():
    base = _tmp_dir()
    try:
        st.save_connection_profile(
            "MySQL", "h", 3306, "db", "u", None, base_dir=base,
            tls=_FakeTls(enabled=False),
        )
        profiles = st.load_connection_profiles(base_dir=base)
        assert profiles[0].tls_enabled is False
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_no_tls_argument_defaults_to_disabled():
    base = _tmp_dir()
    try:
        st.save_connection_profile("Oracle", "h", 1521, "orcl", "scott", "hr", base_dir=base)
        profiles = st.load_connection_profiles(base_dir=base)
        assert profiles[0].tls_enabled is False
        assert profiles[0].tls_verify_cert is True  # dataclass default
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_display_label_marks_a_tls_enabled_profile():
    base = _tmp_dir()
    try:
        st.save_connection_profile(
            "PostgreSQL", "h", 5432, "app", "u", "public", base_dir=base, tls=_FakeTls())
        profiles = st.load_connection_profiles(base_dir=base)
        assert "[TLS]" in profiles[0].display_label
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_profile_saved_before_tls_existed_loads_with_safe_defaults():
    """A profile written by an earlier build has no tls_* keys at all --
    ConnectionProfile(**p) must still construct rather than raising, the
    same forward-compatibility guarantee the SSH fields already have."""
    base = _tmp_dir()
    try:
        path = base / "connection_profiles.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{
            "engine": "Oracle", "host": "h", "port": 1521, "database": "orcl",
            "username": "scott", "last_used": "2020-01-01T00:00:00.000000",
        }]), encoding="utf-8")
        profiles = st.load_connection_profiles(base_dir=base)
        assert len(profiles) == 1
        assert profiles[0].tls_enabled is False
        assert "[TLS]" not in profiles[0].display_label
    finally:
        shutil.rmtree(base, ignore_errors=True)
