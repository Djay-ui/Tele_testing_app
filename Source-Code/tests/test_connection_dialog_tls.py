"""The connection dialog's "Security (TLS/SSL)" group -- tgdatabridge/db/
tls_config.py's enterprise-grade certificate encryption, wired into the
same dialog that already offers an SSH tunnel under "How to reach it".
Mirrors tests/test_connection_dialog_layout.py's own qt_app fixture and
tests/test_ssh_tunnel.py's saved-profile round-trip tests, for TLS
instead of SSH.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tgdatabridge.gui.connection_dialog import ConnectionDialog  # noqa: E402
from tgdatabridge.utils import app_storage  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def test_tls_is_off_by_default(qt_app):
    dialog = ConnectionDialog("PostgreSQL")
    assert dialog.tls_config() is None
    params = dialog.params()
    assert params.tls is None


def test_enabling_tls_produces_a_config(qt_app):
    dialog = ConnectionDialog("PostgreSQL")
    dialog.tls_enabled_check.setChecked(True)
    cfg = dialog.tls_config()
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.verify_cert is True
    assert cfg.verify_hostname is True


def test_unchecking_verify_cert_also_disables_hostname_check(qt_app):
    """Mirrors TlsConfig.validate()'s own rule -- hostname verification
    without certificate verification proves nothing, so the dialog keeps
    the two in sync rather than letting the user create an invalid
    combination that only fails later, at connect time."""
    dialog = ConnectionDialog("PostgreSQL")
    dialog.tls_enabled_check.setChecked(True)
    dialog.tls_verify_cert_check.setChecked(False)
    assert dialog.tls_verify_hostname_check.isChecked() is False
    assert dialog.tls_verify_hostname_check.isEnabled() is False


def test_tls_fields_carry_through_to_params(qt_app):
    dialog = ConnectionDialog("PostgreSQL")
    dialog.tls_enabled_check.setChecked(True)
    dialog.tls_ca_cert_path.setText(" /ca.pem ")
    dialog.tls_client_cert_path.setText("/client.crt")
    dialog.tls_client_key_path.setText("/client.key")
    dialog.tls_client_key_passphrase.setText("secret")
    params = dialog.params()
    assert params.tls.ca_cert_path == "/ca.pem"
    assert params.tls.client_cert_path == "/client.crt"
    assert params.tls.client_key_path == "/client.key"
    assert params.tls.client_key_password == "secret"


@pytest.mark.parametrize("engine", ["Oracle", "MySQL", "PostgreSQL", "MongoDB"])
def test_mutual_tls_fields_are_offered_for_supporting_engines(qt_app, engine):
    dialog = ConnectionDialog(engine)
    dialog.show()  # a widget's own isVisible() is false until its top-level window is shown
    dialog.tls_enabled_check.setChecked(True)
    try:
        assert dialog._tls_client_cert_label.isVisible()
        dialog.tls_client_cert_path.setText("/c.crt")
        dialog.tls_client_key_path.setText("/c.key")
        params = dialog.params()
        assert params.tls.client_cert_path == "/c.crt"
        assert params.tls.client_key_path == "/c.key"
    finally:
        dialog.close()


@pytest.mark.parametrize("engine", ["SQL Server", "DB2"])
def test_mutual_tls_fields_are_hidden_for_unsupported_engines(qt_app, engine):
    """Neither driver here has a client-certificate connection-string
    keyword -- see sqlserver_connector.py's / db2_connector.py's own TLS
    notes. Showing the fields would be filled in and silently ignored."""
    dialog = ConnectionDialog(engine)
    dialog.show()
    dialog.tls_enabled_check.setChecked(True)
    try:
        assert not dialog._tls_client_cert_label.isVisible()
        # Even if something were typed in by hand (e.g. restored oddly), it
        # must not leak into the params this engine's connector receives.
        dialog.tls_client_cert_path.setText("/c.crt")
        dialog.tls_client_key_path.setText("/c.key")
        params = dialog.params()
        assert params.tls.client_cert_path == ""
        assert params.tls.client_key_path == ""
    finally:
        dialog.close()


def test_saved_profile_restores_tls_settings(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(app_storage, "app_data_dir", lambda base=None: tmp_path)
    app_storage.save_connection_profile(
        "PostgreSQL", "db.internal", 5432, "app", "u", "public",
        tls=type("Tls", (), {
            "enabled": True, "verify_cert": True, "verify_hostname": False,
            "ca_cert_path": "/ca.pem", "client_cert_path": "/c.crt",
            "client_key_path": "/c.key",
        })(),
    )
    dialog = ConnectionDialog("PostgreSQL")
    dialog.saved_combo.setCurrentIndex(1)
    assert dialog.tls_enabled_check.isChecked() is True
    assert dialog.tls_verify_hostname_check.isChecked() is False
    assert dialog.tls_ca_cert_path.text() == "/ca.pem"
    assert dialog.tls_client_cert_path.text() == "/c.crt"
    assert dialog.tls_client_key_path.text() == "/c.key"
    # The client key passphrase is never saved.
    assert dialog.tls_client_key_passphrase.text() == ""


def test_accepting_the_dialog_persists_tls_settings(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(app_storage, "app_data_dir", lambda base=None: tmp_path)
    dialog = ConnectionDialog("MySQL")
    dialog.host.setText("mysql.internal")
    dialog.database.setText("app")
    dialog.username.setText("root")
    dialog.tls_enabled_check.setChecked(True)
    dialog.tls_ca_cert_path.setText("/ca.pem")
    dialog.accept()
    profiles = app_storage.load_connection_profiles(engine="MySQL", base_dir=tmp_path)
    assert len(profiles) == 1
    assert profiles[0].tls_enabled is True
    assert profiles[0].tls_ca_cert_path == "/ca.pem"


def test_excel_csv_has_no_tls_group(qt_app):
    """A local file has nothing to encrypt in transit."""
    dialog = ConnectionDialog("Excel/CSV", role="source")
    assert dialog.tls_config() is None
    assert not hasattr(dialog, "tls_group")
