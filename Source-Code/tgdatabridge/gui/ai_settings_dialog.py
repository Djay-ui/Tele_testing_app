"""AI Settings dialog -- where an installation configures the optional
AI-assisted features (see tgdatabridge/ai/ for what they do and
tgdatabridge/utils/ai_settings.py for how this is persisted). Follows the
same style as settings_dialog.py/connection_dialog.py: a QDialog with a
QFormLayout, a standard OK/Cancel button box, and only the fields the
current provider actually needs shown at once (the same discipline
connection_dialog.py's TLS group uses for _update_tls_fields).
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from tgdatabridge.ai.ai_client import AiClient, AiError
from tgdatabridge.ai.ai_config import PROVIDERS
from tgdatabridge.utils import ai_settings

_PROVIDER_LABELS = {
    "claude": "Claude (Anthropic)",
    "openai": "OpenAI",
    "azure_openai": "Azure OpenAI",
    "compatible": "Local / offline server (OpenAI-compatible)",
}
_PROVIDER_LABELS_REVERSE = {v: k for k, v in _PROVIDER_LABELS.items()}


class AiSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Settings")
        self.setMinimumWidth(520)

        self._settings = ai_settings.load_ai_settings()
        self._stored_key_present = ai_settings.api_key_is_stored()
        self._result_settings = self._settings

        intro = QLabel(
            "Turns on optional AI-assisted features: a second opinion on the "
            "automatic column mapping, turning a plain-English request into a "
            "table selection, plain-language error explanations, and a "
            "post-migration data quality review. Off by default -- nothing "
            "here is required, and every feature below can be switched off "
            "independently. Schema/column names and error text are sent to "
            "whichever provider you choose below; the data quality review "
            "additionally sends a small sample of real migrated row values -- "
            "see that feature's own note."
        )
        intro.setWordWrap(True)

        self.enabled_check = QCheckBox("Enable AI-assisted features")
        self.enabled_check.setChecked(self._settings.enabled)
        self.enabled_check.toggled.connect(self._update_fields)

        self.provider_combo = QComboBox()
        self.provider_combo.addItems([_PROVIDER_LABELS[p] for p in PROVIDERS])
        self.provider_combo.setCurrentText(_PROVIDER_LABELS[self._settings.provider])
        self.provider_combo.currentTextChanged.connect(self._update_fields)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(
            "Already saved -- leave blank to keep it" if self._stored_key_present
            else "Not saved yet")

        self.base_url_edit = QLineEdit(self._settings.base_url)
        self.model_edit = QLineEdit(self._settings.model)
        self.azure_api_version_edit = QLineEdit(self._settings.azure_api_version)

        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(1.0, 600.0)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setValue(self._settings.timeout_seconds)

        self.feature_mapping_check = QCheckBox("Schema/column mapping review")
        self.feature_mapping_check.setChecked(self._settings.feature_schema_mapping)
        self.feature_nl_check = QCheckBox("Plain-English migration requests")
        self.feature_nl_check.setChecked(self._settings.feature_nl_config)
        self.feature_errors_check = QCheckBox("Error diagnosis and fix suggestions")
        self.feature_errors_check.setChecked(self._settings.feature_error_diagnostics)
        self.feature_quality_check = QCheckBox("Post-migration data quality review (sends sample row data)")
        self.feature_quality_check.setChecked(self._settings.feature_data_quality)

        self._base_url_label = QLabel("Base URL")
        self._model_label = QLabel("Model")
        self._azure_version_label = QLabel("Azure API version")

        form = QFormLayout()
        form.addRow("Provider", self.provider_combo)
        form.addRow("API key", self.api_key_edit)
        form.addRow(self._base_url_label, self.base_url_edit)
        form.addRow(self._model_label, self.model_edit)
        form.addRow(self._azure_version_label, self.azure_api_version_edit)
        form.addRow("Timeout", self.timeout_spin)
        form.addRow("Features", self.feature_mapping_check)
        form.addRow("", self.feature_nl_check)
        form.addRow("", self.feature_errors_check)
        form.addRow("", self.feature_quality_check)

        test_row_label = QLabel("")
        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(self._test_connection)
        self.test_result_label = QLabel("")
        self.test_result_label.setWordWrap(True)
        form.addRow(test_row_label, self.test_button)
        form.addRow("", self.test_result_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.enabled_check)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._update_fields()

    # ------------------------------------------------------------- helpers

    def _current_provider(self) -> str:
        return _PROVIDER_LABELS_REVERSE.get(self.provider_combo.currentText(), "claude")

    def _update_fields(self) -> None:
        enabled = self.enabled_check.isChecked()
        provider = self._current_provider()
        for widget in (self.provider_combo, self.api_key_edit, self.base_url_edit,
                       self.model_edit, self.timeout_spin, self.feature_mapping_check,
                       self.feature_nl_check, self.feature_errors_check,
                       self.feature_quality_check, self.test_button):
            widget.setEnabled(enabled)

        is_azure = provider == "azure_openai"
        self.azure_api_version_edit.setVisible(is_azure)
        self._azure_version_label.setVisible(is_azure)
        self.azure_api_version_edit.setEnabled(enabled)

        self.model_edit.setPlaceholderText(
            "Your Azure deployment name" if is_azure else "Leave blank for the provider's default")
        self.base_url_edit.setPlaceholderText(
            "Required -- e.g. https://my-resource.openai.azure.com" if is_azure
            else "Required -- e.g. http://localhost:11434" if provider == "compatible"
            else "Leave blank to use the public API")

    def _pending_api_key(self) -> str:
        """What the API key should be treated as right now: a freshly
        typed value takes precedence, otherwise whatever is already
        stored (so leaving the box blank doesn't clear a saved key --
        only an explicit "Clear" would, and this dialog has no such
        button; re-saving with a blank key intentionally keeps it, since
        the placeholder text already tells the user that)."""
        typed = self.api_key_edit.text()
        return typed if typed else ai_settings.load_api_key()

    def _build_config(self):
        from tgdatabridge.ai.ai_config import AiConfig
        return AiConfig(
            enabled=True,  # Test Connection always tries, regardless of the checkbox above.
            provider=self._current_provider(),
            api_key=self._pending_api_key(),
            base_url=self.base_url_edit.text().strip(),
            model=self.model_edit.text().strip(),
            azure_api_version=self.azure_api_version_edit.text().strip() or "2024-06-01",
            timeout_seconds=self.timeout_spin.value(),
        )

    def _test_connection(self) -> None:
        config = self._build_config()
        problem = config.validate()
        if problem:
            self.test_result_label.setText(f"⚠ {problem}")
            return
        self.test_result_label.setText("Testing…")
        try:
            client = AiClient(config)
            reply = client.complete(
                "Reply with exactly one word: OK.", "Are you working?", max_tokens=10)
            self.test_result_label.setText(f"✓ Connected. The model replied: {reply!r}")
        except AiError as exc:
            self.test_result_label.setText(f"⚠ {exc}")

    # ------------------------------------------------------------- accept

    def _on_accept(self) -> None:
        provider = self._current_provider()
        self._result_settings = ai_settings.AiSettings(
            enabled=self.enabled_check.isChecked(),
            provider=provider,
            base_url=self.base_url_edit.text().strip(),
            model=self.model_edit.text().strip(),
            azure_api_version=self.azure_api_version_edit.text().strip() or "2024-06-01",
            timeout_seconds=self.timeout_spin.value(),
            feature_schema_mapping=self.feature_mapping_check.isChecked(),
            feature_nl_config=self.feature_nl_check.isChecked(),
            feature_error_diagnostics=self.feature_errors_check.isChecked(),
            feature_data_quality=self.feature_quality_check.isChecked(),
        )

        if self._result_settings.enabled:
            from tgdatabridge.ai.ai_config import AiConfig
            check = AiConfig(
                enabled=True, provider=provider, api_key=self._pending_api_key(),
                base_url=self._result_settings.base_url, model=self._result_settings.model,
            )
            problem = check.validate()
            if problem:
                QMessageBox.warning(self, "AI Settings", problem)
                return

        typed_key = self.api_key_edit.text()
        if typed_key:
            ai_settings.save_api_key(typed_key)
        ai_settings.save_ai_settings(self._result_settings)
        self.accept()

    def result_settings(self) -> ai_settings.AiSettings:
        return self._result_settings
