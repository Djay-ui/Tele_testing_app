"""The AI Settings dialog -- tgdatabridge/gui/ai_settings_dialog.py.
Mirrors tests/test_connection_dialog_tls.py's own qt_app fixture and
APPDATA-monkeypatching approach (tests/test_app_storage.py) so nothing
here touches the real per-user app data directory.

Every read/write of AI settings in this file goes through the *bare*
tgdatabridge.utils.ai_settings functions (no explicit base_dir) so it
resolves through the same APPDATA-derived path the dialog itself uses --
passing a different base_dir here would silently point the test's
assertions at a different directory than the dialog just wrote to.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from tgdatabridge.ai.ai_client import AiError  # noqa: E402
from tgdatabridge.gui.ai_settings_dialog import AiSettingsDialog  # noqa: E402
from tgdatabridge.utils import ai_settings  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def isolated_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # Never let an unexpected validation failure pop a real, blocking
    # QMessageBox in a headless test run -- individual tests that want to
    # assert on that path patch it back to something they can inspect.
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    return tmp_path


def test_ai_is_off_by_default(qt_app, isolated_appdata):
    dialog = AiSettingsDialog()
    assert dialog.enabled_check.isChecked() is False


def test_provider_specific_fields_toggle_visibility(qt_app, isolated_appdata):
    dialog = AiSettingsDialog()
    dialog.show()
    try:
        dialog.enabled_check.setChecked(True)
        dialog.provider_combo.setCurrentText("Azure OpenAI")
        assert dialog.azure_api_version_edit.isVisible()

        dialog.provider_combo.setCurrentText("Claude (Anthropic)")
        assert not dialog.azure_api_version_edit.isVisible()
    finally:
        dialog.close()


def test_fields_are_disabled_when_ai_is_off(qt_app, isolated_appdata):
    dialog = AiSettingsDialog()
    dialog.enabled_check.setChecked(False)
    assert dialog.provider_combo.isEnabled() is False
    assert dialog.api_key_edit.isEnabled() is False


def test_accepting_persists_settings_and_the_api_key(qt_app, isolated_appdata):
    dialog = AiSettingsDialog()
    dialog.enabled_check.setChecked(True)
    dialog.provider_combo.setCurrentText("OpenAI")
    dialog.api_key_edit.setText("sk-test-123")
    dialog.model_edit.setText("gpt-4o")
    dialog._on_accept()

    saved = ai_settings.load_ai_settings()
    assert saved.enabled is True
    assert saved.provider == "openai"
    assert saved.model == "gpt-4o"
    assert ai_settings.load_api_key() == "sk-test-123"


def test_accepting_with_an_invalid_config_is_rejected(qt_app, isolated_appdata, monkeypatch):
    shown = []
    from tgdatabridge.gui import ai_settings_dialog as mod
    monkeypatch.setattr(mod.QMessageBox, "warning", lambda *a, **k: shown.append(a))

    dialog = AiSettingsDialog()
    dialog.enabled_check.setChecked(True)
    dialog.provider_combo.setCurrentText("Azure OpenAI")
    dialog.api_key_edit.setText("k")
    dialog.base_url_edit.setText("")  # required for Azure -- left blank
    dialog._on_accept()

    assert shown, "expected a warning dialog for the invalid config"
    # Nothing should have been persisted since accept() bailed out early.
    assert ai_settings.load_ai_settings().enabled is False


def test_leaving_the_api_key_blank_on_re_save_keeps_the_stored_one(qt_app, isolated_appdata):
    ai_settings.save_api_key("already-saved-key")

    dialog = AiSettingsDialog()
    dialog.enabled_check.setChecked(True)
    dialog.api_key_edit.setText("")  # left blank on purpose
    dialog._on_accept()

    assert ai_settings.load_api_key() == "already-saved-key"


def test_test_connection_shows_the_validation_problem_without_calling_the_network(qt_app, isolated_appdata):
    dialog = AiSettingsDialog()
    dialog.enabled_check.setChecked(True)
    dialog.api_key_edit.setText("")  # invalid for claude -- no key
    dialog._test_connection()
    assert "API key" in dialog.test_result_label.text()


def test_test_connection_reports_an_ai_error_from_the_client(qt_app, isolated_appdata, monkeypatch):
    from tgdatabridge.gui import ai_settings_dialog as mod

    class _FailingClient:
        def __init__(self, config):
            pass

        def complete(self, *a, **k):
            raise AiError("simulated failure")

    monkeypatch.setattr(mod, "AiClient", _FailingClient)

    dialog = AiSettingsDialog()
    dialog.enabled_check.setChecked(True)
    dialog.api_key_edit.setText("sk-test")
    dialog._test_connection()
    assert "simulated failure" in dialog.test_result_label.text()


def test_test_connection_shows_success_from_the_client(qt_app, isolated_appdata, monkeypatch):
    from tgdatabridge.gui import ai_settings_dialog as mod

    class _OkClient:
        def __init__(self, config):
            pass

        def complete(self, *a, **k):
            return "OK"

    monkeypatch.setattr(mod, "AiClient", _OkClient)

    dialog = AiSettingsDialog()
    dialog.enabled_check.setChecked(True)
    dialog.api_key_edit.setText("sk-test")
    dialog._test_connection()
    assert "Connected" in dialog.test_result_label.text()
