"""Tests for tgdatabridge.utils.settings -- the small persisted app-settings file
(currently just the optional log-shipping endpoint). Every call uses an
explicit `base_dir` so nothing here ever touches the real user's
AppData/home directory. Plain functions, no pytest fixtures -- matches
test_app_storage.py's fixture-free style."""
import pathlib
import shutil
import tempfile

from tgdatabridge.utils import settings as st


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="tgdatabridge_test_"))


def test_load_settings_defaults_when_nothing_saved():
    base = _tmp_dir()
    try:
        s = st.load_settings(base_dir=base)
        assert s == st.AppSettings()
        assert s.shared_storage_path == ""
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_save_and_load_shared_storage_path_roundtrip():
    base = _tmp_dir()
    try:
        s = st.AppSettings(shared_storage_path=r"\\fileserver\share\TGDataBridge")
        st.save_settings(s, base_dir=base)
        loaded = st.load_settings(base_dir=base)
        assert loaded.shared_storage_path == r"\\fileserver\share\TGDataBridge"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_settings_path_always_uses_local_app_data_dir():
    # settings.json must never itself be redirected by shared_storage_path
    # (see this module's own docstring for why) -- it always resolves via
    # app_storage.local_app_data_dir(), not app_data_dir().
    from tgdatabridge.utils import app_storage
    base = _tmp_dir()
    try:
        st.save_settings(st.AppSettings(shared_storage_path="/somewhere/else"), base_dir=base)
        assert (base / "settings.json").exists()
        assert app_storage.local_app_data_dir(base_dir=base) == base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_save_and_load_settings_roundtrip():
    base = _tmp_dir()
    try:
        s = st.AppSettings(shared_storage_path=r"\\fileserver\share\TGDataBridge")
        st.save_settings(s, base_dir=base)
        loaded = st.load_settings(base_dir=base)
        assert loaded == s
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_settings_survives_corrupted_file():
    base = _tmp_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "settings.json").write_text("{not valid json", encoding="utf-8")
        assert st.load_settings(base_dir=base) == st.AppSettings()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_settings_survives_non_dict_json():
    base = _tmp_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert st.load_settings(base_dir=base) == st.AppSettings()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_settings_ignores_unknown_fields_forward_compat():
    base = _tmp_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "settings.json").write_text(
            '{"shared_storage_path": "/tmp/shared", "some_future_field": 123}',
            encoding="utf-8",
        )
        s = st.load_settings(base_dir=base)
        assert s.shared_storage_path == "/tmp/shared"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_settings_ignores_removed_log_shipping_keys():
    """Central log shipping was removed -- logs are written to the local
    machine only. A settings.json written by an older build still carries
    the three log_shipping_* keys, and must load cleanly rather than
    raising: load_settings drops unknown keys, and they disappear from the
    file on the next save."""
    base = _tmp_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "settings.json").write_text(
            '{"log_shipping_enabled": true, "log_shipping_url": "https://x", '
            '"log_shipping_min_level": "info", "shared_storage_path": "/tmp/shared"}',
            encoding="utf-8")
        s = st.load_settings(base_dir=base)
        assert s.shared_storage_path == "/tmp/shared"
        assert not hasattr(s, "log_shipping_enabled")
    finally:
        shutil.rmtree(base, ignore_errors=True)
