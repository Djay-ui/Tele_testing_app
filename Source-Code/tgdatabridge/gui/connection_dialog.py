"""Connection dialog, reused for both source and target: Oracle,
PostgreSQL, MySQL, SQL Server, DB2, and MongoDB can each now be either.

"Excel/CSV" is the one engine that isn't a network service, so it gets a
different form entirely -- a file picker instead of host/port/username/
password (see connector_factory.FILE_SOURCE_ENGINES). It's source-only,
so this dialog is never opened for it in the target role.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QRadioButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from tgdatabridge.core.connector_factory import FILE_SOURCE_ENGINES
from tgdatabridge.db.access import (ACCESS_DIRECT, ACCESS_MODES, ACCESS_SSH, ACCESS_VPN,
                             MODE_FROM_TOKEN, MODE_TOKENS, direct_failure_hint)
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.ssh_tunnel import (AUTH_KEY, AUTH_METHODS, AUTH_PASSWORD,
                                 SshTunnelConfig)
from tgdatabridge.db.tls_config import TlsConfig
from tgdatabridge.db.spreadsheet_connector import MAX_SOURCE_FILES
from tgdatabridge.utils import app_storage

# Engines whose driver, as wired up in tgdatabridge/db/*_connector.py, can
# present a client certificate for mutual TLS. SQL Server's ODBC driver has
# no client-certificate connection-string keyword for a plain TLS
# handshake, and Db2's CLI keyword set has none either (see each
# connector's own TLS section) -- showing the fields there would be an
# invitation to fill them in and wonder why they're ignored, the same
# reasoning _update_ssh_auth_rows already applies to the SSH form.
_MUTUAL_TLS_ENGINES = ("Oracle", "MySQL", "PostgreSQL", "MongoDB")

_DEFAULT_PORTS = {
    "Oracle": 1521, "PostgreSQL": 5432, "MySQL": 3306, "SQL Server": 1433, "DB2": 50000,
    "MongoDB": 27017,
}

_SPREADSHEET_FILTER = "Spreadsheets (*.xlsx *.xlsm *.csv *.tsv);;All files (*)"


class ConnectionDialog(QDialog):
    """Reusable connection dialog; `engine` is "Oracle", "PostgreSQL", "MySQL", "SQL Server", "DB2", or "MongoDB"."""

    def __init__(self, engine: str, parent=None, role: str = "target"):
        super().__init__(parent)
        self.engine = engine
        self.is_file_engine = engine in FILE_SOURCE_ENGINES
        # Excel/CSV may be given several files at once (up to
        # MAX_SOURCE_FILES). The `database` line edit still shows one path
        # -- the first -- because that is what a saved profile stores and
        # what a single-file job has always used; this holds the whole
        # list when the user multi-selects. Empty means "whatever is typed
        # in the box", so typing a path by hand still works.
        self._selected_files: list = []
        # "target" (default, preserves every existing caller's behavior) or
        # "source" -- purely cosmetic, only affects the schema field's row
        # label below (PostgreSQL is now a valid choice for both source and
        # target, and "Target schema" read oddly the first time it was
        # reused for a *source* PostgreSQL connection).
        self.role = role
        self.setWindowTitle("Excel / CSV File" if self.is_file_engine else f"{engine} Connection")
        self.setMinimumWidth(380 if self.is_file_engine else 460)

        # Saved connections (host/port/database/username/schema only -- see
        # app_storage.ConnectionProfile's docstring for why the password is
        # never part of what's saved/restored here). Loaded fresh on every
        # dialog open so a profile saved in an earlier session shows up
        # immediately without restarting the app.
        self._profiles = app_storage.load_connection_profiles(engine=engine)
        self.saved_combo = QComboBox()
        self.saved_combo.addItem("New connection…", None)
        for profile in self._profiles:
            self.saved_combo.addItem(profile.display_label, profile)
        self.saved_combo.currentIndexChanged.connect(self._apply_selected_profile)

        self.host = QLineEdit("localhost")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(_DEFAULT_PORTS.get(engine, 1521))
        self.database = QLineEdit()
        self.database.setPlaceholderText(
            "Service name" if engine == "Oracle" else "Database name")
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.schema = QLineEdit()
        if engine == "Oracle":
            self.schema.setPlaceholderText("Defaults to username")
        elif engine == "SQL Server":
            self.schema.setPlaceholderText("Defaults to dbo")
        elif engine == "DB2":
            self.schema.setPlaceholderText("Defaults to username")
        else:
            self.schema.setPlaceholderText("Defaults to public")

        form = QFormLayout()
        if self._profiles:
            form.addRow("Saved connection", self.saved_combo)
        if self.is_file_engine:
            # A local file has no host/port/credentials -- the path takes
            # the place of all of them, reusing the same `database` field
            # underneath so ConnectionParams stays one uniform shape for
            # every engine (see SpreadsheetConnector's own docstring).
            self.database.setPlaceholderText("Path to a .xlsx, .xlsm, .csv or .tsv file")
            browse_button = QPushButton("Browse…")
            browse_button.clicked.connect(self._browse_for_file)
            file_row = QWidget()
            file_layout = QHBoxLayout(file_row)
            file_layout.setContentsMargins(0, 0, 0, 0)
            file_layout.addWidget(self.database)
            file_layout.addWidget(browse_button)
            self.database.textEdited.connect(self._on_file_path_edited)
            form.addRow("File(s)", file_row)
            # Multi-select puts only the first path in the box, so without
            # this the other 49 would be invisible.
            self.file_summary = QLabel("")
            self.file_summary.setStyleSheet("color: #666;")
            self.file_summary.setVisible(False)
            form.addRow("", self.file_summary)

            # Only meaningful with several files, so it appears with them.
            # Checked by default: workbooks exported by the same system all
            # have the same sheet names, and without prefixing whichever
            # file was selected first keeps the plain names while the rest
            # get prefixed -- asymmetric, and it changes if the selection
            # order does. See spreadsheet_introspector._resolve_table_name.
            self.prefix_check = QCheckBox("Name each table after its file (file_sheet)")
            self.prefix_check.setChecked(True)
            self.prefix_check.setToolTip(
                "With several files, name every table <file>_<sheet> so tables from "
                "different workbooks can't collide and it's clear which file each came "
                "from.\n\nUnchecked, a table is only renamed when its name is already "
                "taken by an earlier file.")
            self.prefix_check.setVisible(False)
            form.addRow("", self.prefix_check)
            self.schema.setPlaceholderText(
                "Defaults to the file name (or \"spreadsheet\" for several files)")
            form.addRow("Schema to create", self.schema)
        else:
            form.addRow("Host", self.host)
            form.addRow("Port", self.port)
            form.addRow("Database / Service", self.database)
            form.addRow("Username", self.username)
            form.addRow("Password", self.password)
        if not self.is_file_engine and engine in ("Oracle", "PostgreSQL", "SQL Server", "DB2"):
            # Oracle used to be source-only, so this label was hardcoded to
            # "Schema to introspect" regardless of role -- now that Oracle
            # can be a target too, it uses the same role-aware label as
            # the other three engines that already had one.
            form.addRow("Schema to introspect" if role == "source" else "Target schema", self.schema)

        self.remember_checkbox = QCheckBox(
            "Remember this file" if self.is_file_engine
            else "Remember this connection (password never saved)")
        self.remember_checkbox.setChecked(True)

        self.test_button = QPushButton("Check File" if self.is_file_engine else "Test Connection")
        self.test_button.clicked.connect(self._test_connection)

        buttons = self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        # The form scrolls; the buttons do not. Adding "How to reach it"
        # made this dialog tall enough that on a 1080p laptop with a
        # taskbar the OK / Cancel / Test Connection row fell off the
        # bottom of the screen -- and a dialog whose only way out is
        # Alt+F4 is worse than no dialog. Everything above the buttons
        # now lives in a scroll area, and the dialog is capped to what
        # actually fits on the screen it opens on.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addLayout(form)
        if not self.is_file_engine:
            content_layout.addWidget(self._build_ssh_group())
            content_layout.addWidget(self._build_tls_group())
        content_layout.addWidget(self.remember_checkbox)
        content_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidget(content)

        layout = QVBoxLayout(self)
        layout.addWidget(self._scroll, 1)
        layout.addWidget(self.test_button)
        layout.addWidget(buttons)
        self._fit_to_screen()

    def _natural_size(self):
        """How big the dialog wants to be, measured from the *contents* of
        the scroll area rather than from the dialog's own sizeHint.

        A QScrollArea reports a small sizeHint on purpose -- it is happy
        to be any size and scroll -- so asking the dialog would always
        answer "the height I already am", and revealing the jump-host
        fields would never make room for them.
        """
        content = self._scroll.widget()
        margins = self.layout().contentsMargins()
        spacing = self.layout().spacing() * 2
        chrome = (self.test_button.sizeHint().height()
                  + self._button_box.sizeHint().height()
                  + margins.top() + margins.bottom() + spacing)
        scrollbar_allowance = 24
        return (content.sizeHint().width() + margins.left() + margins.right()
                + scrollbar_allowance,
                content.sizeHint().height() + chrome)

    def _fit_to_screen(self, grow_only: bool = False) -> None:
        """Size to the form's natural height, but never taller than the
        screen's usable area (which excludes the taskbar), and never
        starting off the top of it.

        `grow_only` is for the re-fit after the "How to reach it" choice
        changes: revealing the jump-host fields should make the dialog
        taller if there is room, but choosing a shorter option again must
        not yank a window the user has resized down to nothing.
        """
        available = None
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
        width, height = self._natural_size()
        width = max(self.minimumWidth(), width)
        if available is not None:
            width = min(width, max(360, available.width() - 80))
            # A little clearance so the dialog's own frame and shadow are
            # inside the usable area too.
            height = min(height, max(320, available.height() - 60))
        if grow_only:
            width = max(width, self.width())
            height = max(height, self.height())
            if available is not None:
                height = min(height, max(320, available.height() - 60))
        self.resize(width, height)
        if available is not None:
            self.setMaximumHeight(available.height())
            frame = self.frameGeometry()
            frame.moveCenter(available.center())
            if frame.top() < available.top():
                frame.moveTop(available.top())
            self.move(frame.topLeft())

    def _apply_selected_profile(self, index: int) -> None:
        profile = self.saved_combo.itemData(index)
        if profile is None:
            return
        self.host.setText(profile.host)
        self.port.setValue(profile.port)
        self.database.setText(profile.database)
        self.username.setText(profile.username)
        self.schema.setText(profile.schema or "")
        self._apply_ssh_profile(profile)
        self._apply_tls_profile(profile)
        # Password is deliberately left as whatever's already in the field
        # (usually blank) -- it's never saved, so there's nothing to restore.

    # ------------------------------------------------------ SSH tunnel

    def _build_ssh_group(self) -> QGroupBox:
        """The bastion / jump-host form.

        A private RDS instance has no publicly routable endpoint at all,
        so "type the host and connect" is not an option that exists --
        the only ways in are a VPN (an operating-system route, nothing an
        application can offer) or an SSH jump host, which is this. See
        tgdatabridge/db/ssh_tunnel.py.
        """
        group = QGroupBox("How to reach it")
        self.ssh_group = group

        # The choice is asked outright rather than left implied by a
        # checkbox, because "is this database public or private?" is the
        # first thing that decides whether a connection can work at all,
        # and the answer changes what the rest of the form even means.
        self.access_direct_radio = QRadioButton(ACCESS_DIRECT)
        self.access_direct_radio.setToolTip(
            "The default, and what this tool has always done: dial the host and port above "
            "straight from this machine.")
        self.access_ssh_radio = QRadioButton(ACCESS_SSH)
        self.access_ssh_radio.setToolTip(
            "For a database with no route from this machine -- an AWS RDS instance in a "
            "private subnet, for example, which has no public endpoint at all.\n\n"
            "The tool opens an SSH connection to a host that CAN reach it (the bastion) and "
            "forwards a local port to the database, the same as running\n"
            "    ssh -N -L 5433:mydb.eu-west-1.rds.amazonaws.com:5432 ec2-user@bastion\n"
            "and connecting to localhost:5433.")
        self.access_vpn_radio = QRadioButton(ACCESS_VPN)
        self.access_vpn_radio.setToolTip(
            "For a private database you can already reach because a VPN client (OpenVPN, "
            "AWS Client VPN, WireGuard, IPsec) has put the route in place.\n\n"
            "The connection itself is then an ordinary direct one -- a VPN is an "
            "operating-system route and no application can bring it up for you. Choosing "
            "this tells the tool to check the route before connecting, so a VPN that has "
            "dropped is reported as exactly that instead of as a database timeout.")
        self.access_direct_radio.setChecked(True)
        for radio in (self.access_direct_radio, self.access_ssh_radio, self.access_vpn_radio):
            radio.toggled.connect(self._update_access_mode)

        self.ssh_host = QLineEdit()
        self.ssh_host.setPlaceholderText("bastion.example.com or 13.234.x.x")
        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(22)
        self.ssh_username = QLineEdit()
        self.ssh_username.setPlaceholderText("ec2-user (Amazon Linux) / ubuntu / admin")
        self.ssh_auth = QComboBox()
        self.ssh_auth.addItems(list(AUTH_METHODS))
        self.ssh_auth.currentTextChanged.connect(self._update_ssh_auth_rows)

        self.ssh_key_path = QLineEdit()
        self.ssh_key_path.setPlaceholderText("C:\\Users\\you\\Downloads\\bastion-key.pem")
        ssh_browse = QPushButton("Browse…")
        ssh_browse.clicked.connect(self._browse_for_key)
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.ssh_key_path)
        key_layout.addWidget(ssh_browse)

        self.ssh_passphrase = QLineEdit()
        self.ssh_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.ssh_passphrase.setPlaceholderText("Only if the key is passphrase-protected")
        self.ssh_password = QLineEdit()
        self.ssh_password.setEchoMode(QLineEdit.EchoMode.Password)

        self.ssh_remote_host = QLineEdit()
        self.ssh_remote_host.setPlaceholderText("Leave blank to use the Host above")
        self.ssh_remote_host.setToolTip(
            "The database address as the bastion sees it -- usually the RDS endpoint, which "
            "only resolves inside the VPC.\n\nLeave it blank if you already typed that "
            "endpoint in Host above; fill it in if Host holds something this machine can "
            "resolve but the bastion cannot, or the other way round.")
        self.ssh_remote_port = QSpinBox()
        self.ssh_remote_port.setRange(0, 65535)
        self.ssh_remote_port.setValue(0)
        self.ssh_remote_port.setSpecialValueText("Same as Port above")

        self.ssh_verify_host_key = QCheckBox("Verify the jump host's key against known_hosts")
        self.ssh_verify_host_key.setToolTip(
            "Off by default because a bastion's key is not in this machine's known_hosts the "
            "first time and there would be no way to accept it from here.\n\nTick it once the "
            "host is in known_hosts and the tunnel will refuse to talk to anything answering "
            "with a different key.")

        ssh_form = QFormLayout()
        ssh_form.addRow("SSH host", self.ssh_host)
        ssh_form.addRow("SSH port", self.ssh_port)
        ssh_form.addRow("SSH username", self.ssh_username)
        ssh_form.addRow("Authentication", self.ssh_auth)
        self._ssh_key_label = QLabel("Private key file")
        ssh_form.addRow(self._ssh_key_label, key_row)
        self._ssh_passphrase_label = QLabel("Key passphrase")
        ssh_form.addRow(self._ssh_passphrase_label, self.ssh_passphrase)
        self._ssh_password_label = QLabel("SSH password")
        ssh_form.addRow(self._ssh_password_label, self.ssh_password)
        ssh_form.addRow("Database host", self.ssh_remote_host)
        ssh_form.addRow("Database port", self.ssh_remote_port)
        ssh_form.addRow("", self.ssh_verify_host_key)

        hint = QLabel(
            "Nothing here is saved except the key file's path — the SSH password and the "
            "key passphrase are re-entered each time, exactly like the database password.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")

        self._ssh_details = QWidget()
        details_layout = QVBoxLayout(self._ssh_details)
        details_layout.setContentsMargins(18, 0, 0, 0)
        details_layout.addLayout(ssh_form)
        details_layout.addWidget(hint)

        self._vpn_note = QLabel(
            "Nothing else to fill in — enter the private endpoint in Host above and "
            "connect. Make sure the VPN is connected first; the tool will check the "
            "route and say so if it is not.")
        self._vpn_note.setWordWrap(True)
        self._vpn_note.setContentsMargins(18, 0, 0, 0)
        self._vpn_note.setStyleSheet("color: #666;")

        box = QVBoxLayout(group)
        box.addWidget(self.access_direct_radio)
        box.addWidget(self.access_ssh_radio)
        box.addWidget(self._ssh_details)
        box.addWidget(self.access_vpn_radio)
        box.addWidget(self._vpn_note)
        self._update_ssh_auth_rows(self.ssh_auth.currentText())
        self._update_access_mode()
        return group

    def _update_access_mode(self) -> None:
        """Show only the fields the chosen answer needs -- an SSH form on a
        public connection is noise, and worse, an invitation to fill it in
        and wonder why it is ignored."""
        self._ssh_details.setVisible(self.access_ssh_radio.isChecked())
        self._vpn_note.setVisible(self.access_vpn_radio.isChecked())
        # Belt and braces on top of the radio indicator: on some Windows
        # themes a selected radio is a small, low-contrast dot, and this
        # choice is too consequential to have to squint at.
        for radio in (self.access_direct_radio, self.access_ssh_radio,
                      self.access_vpn_radio):
            font = radio.font()
            font.setBold(radio.isChecked())
            radio.setFont(font)
        # Revealing the jump-host fields should make room for them when
        # the screen has it, rather than leaving the user to scroll a
        # dialog that could simply have been taller.
        if getattr(self, "_scroll", None) is not None:
            self._scroll.widget().adjustSize()
            self._fit_to_screen(grow_only=True)

    def access_mode(self) -> str:
        if self.is_file_engine or not getattr(self, "ssh_group", None):
            return ACCESS_DIRECT
        if self.access_ssh_radio.isChecked():
            return ACCESS_SSH
        if self.access_vpn_radio.isChecked():
            return ACCESS_VPN
        return ACCESS_DIRECT

    def _update_ssh_auth_rows(self, method: str) -> None:
        """Only the fields the chosen method actually uses -- a password
        box on a key login is an invitation to fill it in and wonder why
        it is ignored."""
        is_key = method == AUTH_KEY
        is_password = method == AUTH_PASSWORD
        for widget in (self._ssh_key_label, self.ssh_key_path.parentWidget(),
                       self._ssh_passphrase_label, self.ssh_passphrase):
            widget.setVisible(is_key)
        for widget in (self._ssh_password_label, self.ssh_password):
            widget.setVisible(is_password)

    def _browse_for_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select the private key for the jump host",
            self.ssh_key_path.text().strip(),
            "Private keys (*.pem *.key id_rsa id_ed25519);;All files (*)")
        if path:
            self.ssh_key_path.setText(path)

    def ssh_config(self) -> Optional[SshTunnelConfig]:
        """None when the box is unticked, so a direct connection carries
        no tunnel object at all and behaves exactly as it always has."""
        if self.is_file_engine or not getattr(self, "ssh_group", None):
            return None
        if not self.access_ssh_radio.isChecked():
            return None
        return SshTunnelConfig(
            enabled=True,
            host=self.ssh_host.text().strip(),
            port=self.ssh_port.value(),
            username=self.ssh_username.text().strip(),
            auth_method=self.ssh_auth.currentText(),
            private_key_path=self.ssh_key_path.text().strip(),
            private_key_passphrase=self.ssh_passphrase.text(),
            password=self.ssh_password.text(),
            remote_host=self.ssh_remote_host.text().strip(),
            remote_port=self.ssh_remote_port.value(),
            verify_host_key=self.ssh_verify_host_key.isChecked(),
        )

    def _apply_ssh_profile(self, profile) -> None:
        if self.is_file_engine or not getattr(self, "ssh_group", None):
            return
        mode = MODE_FROM_TOKEN.get(
            getattr(profile, "access_mode", "") or "",
            ACCESS_SSH if getattr(profile, "ssh_enabled", False) else ACCESS_DIRECT)
        if mode not in ACCESS_MODES:
            mode = ACCESS_DIRECT
        self.access_direct_radio.setChecked(mode == ACCESS_DIRECT)
        self.access_ssh_radio.setChecked(mode == ACCESS_SSH)
        self.access_vpn_radio.setChecked(mode == ACCESS_VPN)
        self._update_access_mode()
        if mode != ACCESS_SSH:
            return
        self.ssh_host.setText(getattr(profile, "ssh_host", "") or "")
        self.ssh_port.setValue(int(getattr(profile, "ssh_port", 22) or 22))
        self.ssh_username.setText(getattr(profile, "ssh_username", "") or "")
        method = getattr(profile, "ssh_auth_method", "") or AUTH_KEY
        if method in AUTH_METHODS:
            self.ssh_auth.setCurrentText(method)
        self.ssh_key_path.setText(getattr(profile, "ssh_private_key_path", "") or "")
        self.ssh_remote_host.setText(getattr(profile, "ssh_remote_host", "") or "")
        self.ssh_remote_port.setValue(int(getattr(profile, "ssh_remote_port", 0) or 0))
        self.ssh_verify_host_key.setChecked(
            bool(getattr(profile, "ssh_verify_host_key", False)))
        # The passphrase and the SSH password are never saved, so they are
        # left as whatever is in the fields -- normally blank.

    # ------------------------------------------------------ TLS / SSL

    def _build_tls_group(self) -> QGroupBox:
        """Certificate-based encryption -- a separate question from "How
        to reach it" above, and the two compose: a bastion-tunneled
        connection can also be encrypted end-to-end. See
        tgdatabridge/db/tls_config.py.
        """
        group = QGroupBox("Security (TLS/SSL)")
        self.tls_group = group

        self.tls_enabled_check = QCheckBox("Encrypt this connection (TLS/SSL)")
        self.tls_enabled_check.setToolTip(
            "Enterprise-grade, certificate-based encryption for the connection itself -- "
            "separate from, and compatible with, an SSH tunnel above.\n\nOff is what this "
            "tool has always done: an unencrypted connection, or (SQL Server only) an "
            "encrypted-but-unverified one -- see the SQL Server connector's own notes.")
        self.tls_enabled_check.toggled.connect(self._update_tls_fields)

        self.tls_verify_cert_check = QCheckBox("Verify server certificate")
        self.tls_verify_cert_check.setChecked(True)
        self.tls_verify_cert_check.setToolTip(
            "Check the server's certificate against a trusted CA. Off still encrypts the "
            "connection but only defends against a passive eavesdropper, not against a "
            "server impersonating the real one -- turn this off only for a self-signed "
            "test database with no CA to verify against.")
        self.tls_verify_cert_check.toggled.connect(self._update_tls_fields)

        self.tls_verify_hostname_check = QCheckBox("Verify hostname matches certificate")
        self.tls_verify_hostname_check.setChecked(True)
        self.tls_verify_hostname_check.setToolTip(
            "Also check that the certificate's own name matches the server being "
            "connected to. Requires \"Verify server certificate\" above.\n\nWhen this "
            "connection goes through an SSH tunnel, the real database address is used for "
            "this check (not the local tunnel port) wherever the driver allows it -- "
            "PostgreSQL and SQL Server can; Oracle, MySQL and DB2 cannot, and silently skip "
            "the hostname check for a tunneled connection even with this ticked. See "
            "tgdatabridge/db/tls_config.py.")

        self.tls_ca_cert_path = QLineEdit()
        self.tls_ca_cert_path.setPlaceholderText("Leave blank to use the system trust store")
        ca_browse = QPushButton("Browse…")
        ca_browse.clicked.connect(self._browse_for_ca_cert)
        ca_row = QWidget()
        ca_layout = QHBoxLayout(ca_row)
        ca_layout.setContentsMargins(0, 0, 0, 0)
        ca_layout.addWidget(self.tls_ca_cert_path)
        ca_layout.addWidget(ca_browse)

        self.tls_client_cert_path = QLineEdit()
        self.tls_client_cert_path.setPlaceholderText("Optional -- for mutual TLS")
        client_cert_browse = QPushButton("Browse…")
        client_cert_browse.clicked.connect(self._browse_for_client_cert)
        client_cert_row = QWidget()
        client_cert_layout = QHBoxLayout(client_cert_row)
        client_cert_layout.setContentsMargins(0, 0, 0, 0)
        client_cert_layout.addWidget(self.tls_client_cert_path)
        client_cert_layout.addWidget(client_cert_browse)

        self.tls_client_key_path = QLineEdit()
        self.tls_client_key_path.setPlaceholderText("Required if a client certificate is set")
        client_key_browse = QPushButton("Browse…")
        client_key_browse.clicked.connect(self._browse_for_client_key)
        client_key_row = QWidget()
        client_key_layout = QHBoxLayout(client_key_row)
        client_key_layout.setContentsMargins(0, 0, 0, 0)
        client_key_layout.addWidget(self.tls_client_key_path)
        client_key_layout.addWidget(client_key_browse)

        self.tls_client_key_passphrase = QLineEdit()
        self.tls_client_key_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.tls_client_key_passphrase.setPlaceholderText("Only if the client key is passphrase-protected")

        tls_form = QFormLayout()
        tls_form.addRow("", self.tls_verify_cert_check)
        tls_form.addRow("", self.tls_verify_hostname_check)
        tls_form.addRow("CA certificate", ca_row)
        self._tls_client_cert_label = QLabel("Client certificate")
        tls_form.addRow(self._tls_client_cert_label, client_cert_row)
        self._tls_client_key_label = QLabel("Client private key")
        tls_form.addRow(self._tls_client_key_label, client_key_row)
        self._tls_client_key_passphrase_label = QLabel("Key passphrase")
        tls_form.addRow(self._tls_client_key_passphrase_label, self.tls_client_key_passphrase)

        hint = QLabel(
            "Nothing here is saved except the certificate/key file paths — the client "
            "key's passphrase is re-entered each time, exactly like the database "
            "password.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")

        self._tls_details = QWidget()
        details_layout = QVBoxLayout(self._tls_details)
        details_layout.setContentsMargins(18, 0, 0, 0)
        details_layout.addLayout(tls_form)
        details_layout.addWidget(hint)

        box = QVBoxLayout(group)
        box.addWidget(self.tls_enabled_check)
        box.addWidget(self._tls_details)
        self._update_tls_fields()
        return group

    def _update_tls_fields(self) -> None:
        """Show only the fields the current choice actually uses -- same
        discipline as _update_ssh_auth_rows / _update_access_mode."""
        enabled = self.tls_enabled_check.isChecked()
        self._tls_details.setVisible(enabled)
        self.tls_verify_hostname_check.setEnabled(self.tls_verify_cert_check.isChecked())
        if not self.tls_verify_cert_check.isChecked():
            self.tls_verify_hostname_check.setChecked(False)
        mutual_tls_supported = self.engine in _MUTUAL_TLS_ENGINES
        for widget in (self._tls_client_cert_label, self.tls_client_cert_path.parentWidget(),
                       self._tls_client_key_label, self.tls_client_key_path.parentWidget(),
                       self._tls_client_key_passphrase_label, self.tls_client_key_passphrase):
            widget.setVisible(mutual_tls_supported)
        if getattr(self, "_scroll", None) is not None:
            self._scroll.widget().adjustSize()
            self._fit_to_screen(grow_only=True)

    def _browse_for_ca_cert(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select the CA certificate", self.tls_ca_cert_path.text().strip(),
            "Certificates (*.pem *.crt *.cer);;All files (*)")
        if path:
            self.tls_ca_cert_path.setText(path)

    def _browse_for_client_cert(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select the client certificate", self.tls_client_cert_path.text().strip(),
            "Certificates (*.pem *.crt *.cer);;All files (*)")
        if path:
            self.tls_client_cert_path.setText(path)

    def _browse_for_client_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select the client private key", self.tls_client_key_path.text().strip(),
            "Private keys (*.pem *.key);;All files (*)")
        if path:
            self.tls_client_key_path.setText(path)

    def tls_config(self) -> Optional[TlsConfig]:
        """None when the box is unticked, so an unencrypted connection
        carries no TLS object at all and behaves exactly as it always
        has -- mirrors ssh_config()'s own contract."""
        if self.is_file_engine or not getattr(self, "tls_group", None):
            return None
        if not self.tls_enabled_check.isChecked():
            return None
        mutual = self.engine in _MUTUAL_TLS_ENGINES
        return TlsConfig(
            enabled=True,
            verify_cert=self.tls_verify_cert_check.isChecked(),
            verify_hostname=self.tls_verify_hostname_check.isChecked(),
            ca_cert_path=self.tls_ca_cert_path.text().strip(),
            client_cert_path=self.tls_client_cert_path.text().strip() if mutual else "",
            client_key_path=self.tls_client_key_path.text().strip() if mutual else "",
            client_key_password=self.tls_client_key_passphrase.text() if mutual else "",
        )

    def _apply_tls_profile(self, profile) -> None:
        if self.is_file_engine or not getattr(self, "tls_group", None):
            return
        self.tls_enabled_check.setChecked(bool(getattr(profile, "tls_enabled", False)))
        self.tls_verify_cert_check.setChecked(bool(getattr(profile, "tls_verify_cert", True)))
        self.tls_verify_hostname_check.setChecked(bool(getattr(profile, "tls_verify_hostname", True)))
        self.tls_ca_cert_path.setText(getattr(profile, "tls_ca_cert_path", "") or "")
        self.tls_client_cert_path.setText(getattr(profile, "tls_client_cert_path", "") or "")
        self.tls_client_key_path.setText(getattr(profile, "tls_client_key_path", "") or "")
        self._update_tls_fields()
        # The client key passphrase is never saved, so it is left as
        # whatever is in the field -- normally blank.

    def _browse_for_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select one or more spreadsheets", self.database.text().strip(),
            _SPREADSHEET_FILTER)
        if not paths:
            return
        if len(paths) > MAX_SOURCE_FILES:
            QMessageBox.warning(
                self, "Too many files",
                f"{len(paths)} files were selected, but at most {MAX_SOURCE_FILES} can be read "
                f"in one migration.\n\nOnly the first {MAX_SOURCE_FILES} were kept -- remove "
                "some, or split the job into several runs.",
            )
            paths = paths[:MAX_SOURCE_FILES]
        self._selected_files = list(paths)
        self.database.setText(paths[0])
        self._update_file_summary()

    def _update_file_summary(self) -> None:
        count = len(self._selected_files)
        multi = count > 1
        if multi:
            self.file_summary.setText(
                f"{count} files selected — every sheet in all of them becomes a table "
                "in one schema.")
        self.file_summary.setVisible(multi)
        self.prefix_check.setVisible(multi)

    def _on_file_path_edited(self) -> None:
        """Typing a path by hand replaces a previous multi-selection --
        otherwise the box would show one file while the job silently ran
        against the fifty picked earlier."""
        self._selected_files = []
        self._update_file_summary()

    def accept(self) -> None:
        if self.remember_checkbox.isChecked():
            params = self.params()
            # A file engine has no host to check -- the path alone is the
            # whole "connection", so that's what decides whether there's
            # anything worth remembering.
            worth_saving = bool(params.database) if self.is_file_engine else bool(params.host and params.database)
            if worth_saving:
                try:
                    app_storage.save_connection_profile(
                        self.engine, params.host, params.port, params.database,
                        params.username, params.schema, ssh=params.ssh,
                        access_mode=MODE_TOKENS.get(params.access_mode, "public"),
                        tls=params.tls,
                    )
                except OSError:
                    # Persisting a profile is a convenience, not something
                    # that should ever block completing an otherwise-valid
                    # connection over a disk/permissions problem.
                    pass
        super().accept()

    def params(self) -> ConnectionParams:
        schema_value: Optional[str] = None
        if self.is_file_engine:
            # blank -> None; SpreadsheetConnector.schema_name falls back to
            # the file's own stem.
            # None (rather than the checkbox's value) for a single file, so
            # the introspector applies its own "one file keeps plain names"
            # default instead of this hidden checkbox deciding it.
            multi = len(self._selected_files) > 1
            return ConnectionParams(
                host="", port=0, database=self.database.text().strip(), username="", password="",
                schema=self.schema.text().strip() or None,
                files=list(self._selected_files) or None,
                prefix_tables_with_file=self.prefix_check.isChecked() if multi else None,
            )
        if self.engine == "Oracle":
            # blank -> None; OracleConnector.schema_name falls back to the username
            schema_value = self.schema.text().strip() or None
        elif self.engine == "PostgreSQL":
            # blank must actually resolve to "public", not None — leaving this as
            # None skips PostgresConnector's search_path fix-up entirely, which is
            # exactly what caused "no schema has been selected to create in" when
            # the field was left blank trusting the placeholder text alone.
            schema_value = self.schema.text().strip() or "public"
        elif self.engine == "SQL Server":
            # Same reasoning as PostgreSQL above, "dbo" instead of "public" —
            # also what ddl_generator.generate_schema_ddl schema-qualifies
            # every object with when target_schema is passed through.
            schema_value = self.schema.text().strip() or "dbo"
        elif self.engine == "DB2":
            # Same reasoning as SQL Server above, except Db2 has no
            # engine-wide fixed default schema name ("dbo"/"public") --
            # its own default is the connecting user's ID, so a blank field
            # resolves to that (upper-cased, matching Db2's identifier
            # folding) instead, mirroring Db2Connector.schema_name's own
            # fallback. If username is also blank there's nothing sensible
            # to default to, so this falls through to None like Oracle.
            schema_value = self.schema.text().strip() or self.username.text().strip().upper() or None

        return ConnectionParams(
            host=self.host.text().strip(),
            port=self.port.value(),
            database=self.database.text().strip(),
            username=self.username.text().strip(),
            password=self.password.text(),
            schema=schema_value,
            ssh=self.ssh_config(),
            access_mode=self.access_mode(),
            tls=self.tls_config(),
        )

    def _connector(self):
        # Deliberately routed through the same factory the GUI's main
        # window and the headless CLI both use, so this dialog's "Test
        # Connection" can never end up exercising a different connector
        # class than the one the actual run will use.
        from tgdatabridge.core.connector_factory import make_source_connector, make_target_connector

        params = self.params()
        if self.role == "source":
            return make_source_connector(self.engine, params)
        return make_target_connector(self.engine, params)

    def _test_connection(self) -> None:
        try:
            connector = self._connector()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Connection failed", str(exc))
            return

        ok, message = connector.test_connection()
        if ok:
            QMessageBox.information(self, "Connection succeeded", message)
            return
        # A direct connection that failed may simply have no route -- the
        # exact case this dialog's "How to reach it" question exists for.
        # The driver cannot tell the difference between "wrong password"
        # and "unreachable", so ask the network directly and say so.
        if self.access_mode() == ACCESS_DIRECT and not self.is_file_engine:
            params = self.params()
            hint = direct_failure_hint(params.host, params.port)
            if hint:
                message += hint
        QMessageBox.critical(self, "Connection failed", message)
