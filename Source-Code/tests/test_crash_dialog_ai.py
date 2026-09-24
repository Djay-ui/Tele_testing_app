"""The crash dialog's optional "Explain with AI" button --
tgdatabridge/gui/crash_dialog.py. Split out from test_crash.py, which
deliberately stays Qt-free (see that module's own docstring); this file
imports PySide6 the same way tests/test_ai_settings_dialog.py does.

_build_ai_explanation_text is tested directly (never through a real,
exec()ed QMessageBox, which would block a headless test run waiting for a
click that never comes -- see _show_ai_explanation's own docstring)."""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from tgdatabridge.ai.ai_client import AiError  # noqa: E402
from tgdatabridge.ai.error_diagnostics import Diagnosis  # noqa: E402
from tgdatabridge.gui import crash_dialog  # noqa: E402
from tgdatabridge.utils import ai_settings  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def isolated_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


# --------------------------------------------------------- button gating


def test_no_explain_button_when_ai_is_off(qt_app, isolated_appdata):
    box = QMessageBox()
    assert crash_dialog._add_explain_button(box) is None


def test_no_explain_button_when_the_feature_flag_is_off(qt_app, isolated_appdata):
    ai_settings.save_ai_settings(ai_settings.AiSettings(enabled=True, feature_error_diagnostics=False))
    box = QMessageBox()
    assert crash_dialog._add_explain_button(box) is None


def test_explain_button_appears_when_enabled(qt_app, isolated_appdata):
    ai_settings.save_ai_settings(ai_settings.AiSettings(enabled=True, feature_error_diagnostics=True))
    box = QMessageBox()
    button = crash_dialog._add_explain_button(box)
    assert button is not None
    assert button.text() == "Explain with AI"


def test_a_broken_ai_settings_read_never_breaks_button_construction(qt_app, isolated_appdata, monkeypatch):
    monkeypatch.setattr(ai_settings, "load_ai_settings", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    box = QMessageBox()
    assert crash_dialog._add_explain_button(box) is None


# -------------------------------------------------------- explanation text


def test_explanation_text_includes_the_diagnosis(qt_app, isolated_appdata, monkeypatch):
    ai_settings.save_ai_settings(ai_settings.AiSettings(enabled=True))
    ai_settings.save_api_key("sk-test")

    def fake_explain_error(client, error_text, context=""):
        return Diagnosis(explanation="The port is wrong.", suggested_fixes=["Check the port.", "Retry."])

    monkeypatch.setattr("tgdatabridge.ai.error_diagnostics.explain_error", fake_explain_error)

    text = crash_dialog._build_ai_explanation_text("ConnectionRefusedError: [Errno 111]")
    assert "The port is wrong." in text
    assert "1. Check the port." in text
    assert "2. Retry." in text


def test_explanation_text_reports_an_ai_error_plainly(qt_app, isolated_appdata, monkeypatch):
    ai_settings.save_ai_settings(ai_settings.AiSettings(enabled=True))
    ai_settings.save_api_key("sk-test")

    def raiser(client, error_text, context=""):
        raise AiError("could not reach the service")

    monkeypatch.setattr("tgdatabridge.ai.error_diagnostics.explain_error", raiser)

    text = crash_dialog._build_ai_explanation_text("some crash summary")
    assert "could not reach the service" in text


def test_context_passed_to_explain_error_names_the_product(qt_app, isolated_appdata, monkeypatch):
    ai_settings.save_ai_settings(ai_settings.AiSettings(enabled=True))
    ai_settings.save_api_key("sk-test")
    captured = {}

    def fake_explain_error(client, error_text, context=""):
        captured["context"] = context
        return Diagnosis(explanation="x", suggested_fixes=[])

    monkeypatch.setattr("tgdatabridge.ai.error_diagnostics.explain_error", fake_explain_error)
    crash_dialog._build_ai_explanation_text("boom")
    assert "crash" in captured["context"]
