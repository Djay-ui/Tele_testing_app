"""Reaching a private database through a bastion / jump host.

An AWS RDS instance in a private subnet has no publicly routable
endpoint, so there is nothing to type into Host that would work from a
laptop. The tool now opens the same SSH port-forward a person would open
by hand and connects to its local end.
"""

import pytest

from tgdatabridge.core.connector_factory import _resolve
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.ssh_tunnel import (AUTH_KEY, AUTH_PASSWORD, SshTunnelConfig,
                                 SshTunnelError, TunnelManager, _explain,
                                 resolve_params)


def _cfg(**overrides):
    base = dict(enabled=True, host="bastion.example.com", port=22,
                username="ec2-user", auth_method=AUTH_KEY,
                private_key_path=__file__)  # any file that exists
    base.update(overrides)
    return SshTunnelConfig(**base)


# ------------------------------------------------------------ validation

def test_a_disabled_tunnel_validates_and_changes_nothing():
    params = ConnectionParams("db.internal", 5432, "app", "u", "p",
                              ssh=SshTunnelConfig(enabled=False))
    assert SshTunnelConfig(enabled=False).validate() is None
    assert resolve_params(params) is params


def test_no_tunnel_at_all_is_the_untouched_path():
    params = ConnectionParams("db.internal", 5432, "app", "u", "p")
    assert params.ssh is None
    assert _resolve(params) is params


def test_missing_pieces_are_named_before_anything_is_dialled():
    assert "SSH host" in _cfg(host="").validate()
    assert "username" in _cfg(username="").validate()
    assert "private key" in _cfg(private_key_path="").validate().lower()
    assert "not found" in _cfg(private_key_path="/no/such/key.pem").validate()
    assert "password" in _cfg(auth_method=AUTH_PASSWORD, password="").validate().lower()
    assert _cfg(auth_method=AUTH_PASSWORD, password="x").validate() is None
    assert _cfg().validate() is None


def test_an_invalid_config_fails_before_a_connection_is_attempted():
    params = ConnectionParams("db.internal", 5432, "app", "u", "p",
                              ssh=_cfg(host=""))
    with pytest.raises(SshTunnelError) as caught:
        _resolve(params)
    assert "SSH host" in str(caught.value)


def test_there_has_to_be_something_to_forward_to():
    manager = TunnelManager()
    with pytest.raises(SshTunnelError) as caught:
        manager.ensure(_cfg(), "", 0)
    assert "no database address" in str(caught.value).lower()


# ------------------------------------------------------------- messages

@pytest.mark.parametrize("text,expected", [
    ("Permission denied (publickey)", "refused the login"),
    ("Host key is not trusted", "not trusted"),
    ("OpenSSH private key encryption requires bcrypt with KDF support", "ssh-keygen -p"),
    ("Connection refused", "Could not reach the jump host"),
    ("[Errno 8] nodename nor servname provided (getaddrinfo)", "Could not reach the jump host"),
    ("Timed out connecting", "security group"),
    ("open failed: administratively prohibited", "refused to forward"),
])
def test_a_failure_says_what_to_fix(text, expected):
    message = _explain(RuntimeError(text), _cfg())
    assert expected in message
    # the driver's own words are kept too, for anyone who needs them
    assert text.split()[0] in message


def test_the_bcrypt_message_names_the_key_and_the_way_out():
    message = _explain(
        RuntimeError("OpenSSH private key encryption requires bcrypt with KDF support"),
        _cfg(private_key_path="C:/keys/id_ed25519"))
    assert "C:/keys/id_ed25519" in message
    assert "ssh-keygen -p" in message
    assert "PEM" in message


# --------------------------------------------------------- reuse / close

class _FakeTunnel:
    instances = 0

    def __init__(self):
        _FakeTunnel.instances += 1
        self.local_host = "127.0.0.1"
        self.local_port = 40000 + _FakeTunnel.instances
        self.alive = True
        self.closed = False

    def is_alive(self):
        return self.alive

    def close(self):
        self.closed = True


@pytest.fixture
def fake_open(monkeypatch):
    _FakeTunnel.instances = 0
    made = []

    def _open(cfg, remote_host, remote_port):
        tunnel = _FakeTunnel()
        made.append((tunnel, remote_host, remote_port))
        return tunnel, "fake"

    monkeypatch.setattr("tgdatabridge.db.ssh_tunnel._open", _open)
    return made


def test_one_tunnel_is_shared_by_every_connector(fake_open):
    """Connectors are built per operation. Opening a fresh SSH session for
    each would mean re-authenticating with the bastion on every click, and
    the local port would keep changing under the connection."""
    manager = TunnelManager()
    first = manager.ensure(_cfg(), "db.internal", 5432)
    for _ in range(5):
        assert manager.ensure(_cfg(), "db.internal", 5432) == first
    assert len(fake_open) == 1
    assert manager.open_count() == 1


def test_a_different_database_gets_its_own_tunnel(fake_open):
    manager = TunnelManager()
    one = manager.ensure(_cfg(), "db-a.internal", 5432)
    two = manager.ensure(_cfg(), "db-b.internal", 5432)
    assert one != two
    assert manager.open_count() == 2


def test_a_dropped_tunnel_is_rebuilt_rather_than_handed_back(fake_open):
    manager = TunnelManager()
    manager.ensure(_cfg(), "db.internal", 5432)
    fake_open[0][0].alive = False
    manager.ensure(_cfg(), "db.internal", 5432)
    assert len(fake_open) == 2
    assert fake_open[0][0].closed, "the dead one is closed, not leaked"


def test_close_all_shuts_everything_down(fake_open):
    manager = TunnelManager()
    manager.ensure(_cfg(), "db-a.internal", 5432)
    manager.ensure(_cfg(), "db-b.internal", 5432)
    manager.close_all()
    assert manager.open_count() == 0
    assert all(t.closed for t, _h, _p in fake_open)


def test_the_connection_is_re_addressed_to_the_local_end(fake_open, monkeypatch):
    import tgdatabridge.db.ssh_tunnel as mod

    monkeypatch.setattr(mod, "tunnels", TunnelManager())
    params = ConnectionParams("db.internal", 5432, "app", "u", "p", ssh=_cfg())
    resolved = resolve_params(params)
    assert resolved.host == "127.0.0.1"
    assert resolved.port != 5432
    # everything else is carried through untouched
    assert (resolved.database, resolved.username, resolved.password) == ("app", "u", "p")
    # and the original is not mutated -- the dialog still shows what was typed
    assert params.host == "db.internal" and params.port == 5432


def test_the_tunnel_forwards_to_the_connection_host_by_default(fake_open, monkeypatch):
    import tgdatabridge.db.ssh_tunnel as mod

    monkeypatch.setattr(mod, "tunnels", TunnelManager())
    resolve_params(ConnectionParams("db.internal", 5432, "app", "u", "p", ssh=_cfg()))
    _tunnel, remote_host, remote_port = fake_open[0]
    assert (remote_host, remote_port) == ("db.internal", 5432)


def test_an_explicit_database_host_overrides_it(fake_open, monkeypatch):
    """For the case where Host holds something this machine resolves but
    the bastion does not, or the other way round."""
    import tgdatabridge.db.ssh_tunnel as mod

    monkeypatch.setattr(mod, "tunnels", TunnelManager())
    cfg = _cfg(remote_host="mydb.abc.eu-west-1.rds.amazonaws.com", remote_port=5432)
    resolve_params(ConnectionParams("whatever", 1234, "app", "u", "p", ssh=cfg))
    _tunnel, remote_host, remote_port = fake_open[0]
    assert (remote_host, remote_port) == ("mydb.abc.eu-west-1.rds.amazonaws.com", 5432)


# --------------------------------------------------- combined with TLS/SSL

def test_tls_hostname_is_pinned_to_the_real_host_before_the_tunnel_rewrites_it(
        fake_open, monkeypatch):
    """The whole reason connector_factory._pin_tls_hostname has to run
    ahead of the SSH resolve step, not after it -- see tgdatabridge/db/
    tls_config.py's module docstring. Proven here end-to-end through
    connector_factory._resolve, the single chokepoint every connector
    actually goes through, rather than only against the two pieces in
    isolation."""
    import tgdatabridge.db.ssh_tunnel as mod
    from tgdatabridge.db.tls_config import TlsConfig

    monkeypatch.setattr(mod, "tunnels", TunnelManager())
    params = ConnectionParams(
        "db.internal", 5432, "app", "u", "p", ssh=_cfg(),
        tls=TlsConfig(enabled=True, verify_hostname=True))
    resolved = _resolve(params)
    # dialed at the tunnel's local end...
    assert resolved.host == "127.0.0.1"
    assert resolved.port != 5432
    # ...but the certificate is still checked against the real address.
    assert resolved.tls.server_host_override == "db.internal"


# ------------------------------------------------------- saved profiles

def test_the_profile_saves_the_jump_host_but_never_its_secrets(tmp_path):
    from tgdatabridge.utils import app_storage

    cfg = _cfg(private_key_passphrase="hunter2", password="hunter2")
    app_storage.save_connection_profile(
        "MySQL", "db.internal", 3306, "app", "u", None, base_dir=tmp_path, ssh=cfg)
    (saved,) = app_storage.load_connection_profiles("MySQL", base_dir=tmp_path)
    assert saved.ssh_enabled and saved.ssh_host == "bastion.example.com"
    assert saved.ssh_username == "ec2-user"
    assert saved.ssh_private_key_path == __file__
    blob = (tmp_path / "connection_profiles.json").read_text(encoding="utf-8")
    assert "hunter2" not in blob
    assert "bastion.example.com" in saved.display_label


def test_direct_and_tunnelled_connections_are_separate_profiles(tmp_path):
    from tgdatabridge.utils import app_storage

    app_storage.save_connection_profile(
        "MySQL", "db.internal", 3306, "app", "u", None, base_dir=tmp_path)
    app_storage.save_connection_profile(
        "MySQL", "db.internal", 3306, "app", "u", None, base_dir=tmp_path, ssh=_cfg())
    assert len(app_storage.load_connection_profiles("MySQL", base_dir=tmp_path)) == 2


def test_a_profile_written_before_this_feature_still_loads(tmp_path):
    import json

    from tgdatabridge.utils import app_storage

    path = tmp_path / "connection_profiles.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{
        "engine": "MySQL", "host": "h", "port": 3306, "database": "d",
        "username": "u", "schema": None, "last_used": "2026-01-01T00:00:00",
    }]), encoding="utf-8")
    (profile,) = app_storage.load_connection_profiles("MySQL", base_dir=tmp_path)
    assert profile.ssh_enabled is False


# ------------------------------------------------------------- CLI config

def _job(ssh_block):
    return {
        "source": {"engine": "MySQL", "host": "db.internal", "port": 3306,
                   "database": "app", "username": "u", "password_env": "SRC_PW",
                   "ssh": ssh_block},
        "target": {"engine": "PostgreSQL", "host": "t", "port": 5432,
                   "database": "app", "username": "u", "password_env": "TGT_PW"},
    }


def test_the_cli_reads_an_ssh_block(monkeypatch, tmp_path):
    import json

    from tgdatabridge.cli import config as cli_config

    monkeypatch.setenv("SRC_PW", "dbpass")
    monkeypatch.setenv("BASTION_PASSPHRASE", "keypass")
    path = tmp_path / "job.json"
    path.write_text(json.dumps(_job({
        "host": "bastion.example.com", "username": "ec2-user",
        "private_key_path": __file__, "passphrase_env": "BASTION_PASSPHRASE",
        "database_host": "mydb.abc.eu-west-1.rds.amazonaws.com", "database_port": 3306,
    })), encoding="utf-8")
    job = cli_config.load_job_config(str(path))
    params = cli_config.resolve_connection_params(job.source)
    assert params.ssh.enabled
    assert params.ssh.host == "bastion.example.com"
    assert params.ssh.auth_method == AUTH_KEY
    assert params.ssh.private_key_passphrase == "keypass"
    assert params.ssh.remote_host == "mydb.abc.eu-west-1.rds.amazonaws.com"


def test_the_cli_never_takes_a_secret_from_the_file_itself(tmp_path):
    import json

    from tgdatabridge.cli import config as cli_config

    path = tmp_path / "job.json"
    path.write_text(json.dumps(_job({
        "host": "b", "username": "u", "password": "hunter2",
    })), encoding="utf-8")
    with pytest.raises(cli_config.CliConfigError) as caught:
        cli_config.load_job_config(str(path))
    assert "unknown field" in str(caught.value)


def test_the_cli_rejects_an_ssh_block_with_no_way_to_authenticate(tmp_path):
    import json

    from tgdatabridge.cli import config as cli_config

    path = tmp_path / "job.json"
    path.write_text(json.dumps(_job({"host": "b", "username": "u"})), encoding="utf-8")
    with pytest.raises(cli_config.CliConfigError) as caught:
        cli_config.load_job_config(str(path))
    assert "private_key_path" in str(caught.value)


def test_the_cli_says_which_environment_variable_is_missing(monkeypatch, tmp_path):
    import json

    from tgdatabridge.cli import config as cli_config

    monkeypatch.setenv("SRC_PW", "dbpass")
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    path = tmp_path / "job.json"
    path.write_text(json.dumps(_job({
        "host": "b", "username": "u", "password_env": "NOT_SET_ANYWHERE",
    })), encoding="utf-8")
    job = cli_config.load_job_config(str(path))
    with pytest.raises(cli_config.CliConfigError) as caught:
        cli_config.resolve_connection_params(job.source)
    assert "NOT_SET_ANYWHERE" in str(caught.value)


# ------------------------------------------------------- the dialog

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_a_connection_is_public_and_direct_by_default(qt_app):
    from tgdatabridge.db.access import ACCESS_DIRECT
    from tgdatabridge.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog("MySQL")
    assert dialog.access_direct_radio.isChecked()
    assert dialog.access_mode() == ACCESS_DIRECT
    assert dialog.ssh_config() is None
    assert dialog.params().ssh is None


def test_ticking_it_produces_a_config(qt_app):
    from tgdatabridge.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog("PostgreSQL")
    dialog.access_ssh_radio.setChecked(True)
    dialog.ssh_host.setText("bastion.example.com")
    dialog.ssh_username.setText("ec2-user")
    dialog.ssh_key_path.setText(__file__)
    cfg = dialog.params().ssh
    assert cfg.enabled and cfg.host == "bastion.example.com"
    assert cfg.validate() is None


def test_only_the_fields_the_chosen_method_uses_are_shown(qt_app):
    from tgdatabridge.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog("MySQL")
    dialog.access_ssh_radio.setChecked(True)
    dialog.ssh_auth.setCurrentText(AUTH_KEY)
    assert dialog.ssh_passphrase.isVisibleTo(dialog.ssh_group)
    assert not dialog.ssh_password.isVisibleTo(dialog.ssh_group)
    dialog.ssh_auth.setCurrentText(AUTH_PASSWORD)
    assert dialog.ssh_password.isVisibleTo(dialog.ssh_group)
    assert not dialog.ssh_passphrase.isVisibleTo(dialog.ssh_group)


def test_a_file_engine_has_no_tunnel_form_at_all(qt_app):
    from tgdatabridge.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog("Excel/CSV", role="source")
    assert dialog.ssh_config() is None
    assert dialog.params().ssh is None


# ------------------------------------------- public / private / VPN

def test_the_three_ways_of_reaching_a_database_are_offered(qt_app):
    from tgdatabridge.db.access import ACCESS_DIRECT, ACCESS_SSH, ACCESS_VPN
    from tgdatabridge.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog("MySQL")
    labels = [dialog.access_direct_radio.text(), dialog.access_ssh_radio.text(),
              dialog.access_vpn_radio.text()]
    assert labels == [ACCESS_DIRECT, ACCESS_SSH, ACCESS_VPN]
    # exactly one at a time
    dialog.access_vpn_radio.setChecked(True)
    assert not dialog.access_direct_radio.isChecked()
    assert not dialog.access_ssh_radio.isChecked()


def test_the_jump_host_fields_only_appear_for_the_ssh_choice(qt_app):
    from tgdatabridge.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog("MySQL")
    assert not dialog._ssh_details.isVisibleTo(dialog.ssh_group)
    dialog.access_ssh_radio.setChecked(True)
    assert dialog._ssh_details.isVisibleTo(dialog.ssh_group)
    assert not dialog._vpn_note.isVisibleTo(dialog.ssh_group)
    dialog.access_vpn_radio.setChecked(True)
    assert not dialog._ssh_details.isVisibleTo(dialog.ssh_group)
    assert dialog._vpn_note.isVisibleTo(dialog.ssh_group)


def test_the_vpn_choice_carries_no_tunnel(qt_app):
    """A VPN route is the operating system's, so the connection is an
    ordinary direct one -- the choice only changes how failure reads."""
    from tgdatabridge.db.access import ACCESS_VPN
    from tgdatabridge.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog("PostgreSQL")
    dialog.access_vpn_radio.setChecked(True)
    dialog.host.setText("10.0.3.44")
    params = dialog.params()
    assert params.access_mode == ACCESS_VPN
    assert params.ssh is None


def test_a_vpn_connection_with_no_route_says_so(monkeypatch):
    from tgdatabridge.core.connector_factory import _resolve
    from tgdatabridge.db import access as access_mod
    from tgdatabridge.db.access import ACCESS_VPN, NotReachableError

    monkeypatch.setattr(access_mod, "is_reachable", lambda *a, **k: False)
    params = ConnectionParams("10.0.3.44", 5432, "app", "u", "p",
                              access_mode=ACCESS_VPN)
    with pytest.raises(NotReachableError) as caught:
        _resolve(params)
    message = str(caught.value)
    assert "10.0.3.44:5432" in message
    assert "VPN client is connected" in message
    assert "SSH tunnel" in message, "and what to do if there is no VPN"


def test_a_vpn_connection_with_a_route_is_left_alone(monkeypatch):
    from tgdatabridge.core.connector_factory import _resolve
    from tgdatabridge.db import access as access_mod
    from tgdatabridge.db.access import ACCESS_VPN

    monkeypatch.setattr(access_mod, "is_reachable", lambda *a, **k: True)
    params = ConnectionParams("10.0.3.44", 5432, "app", "u", "p",
                              access_mode=ACCESS_VPN)
    assert _resolve(params) is params


def test_a_public_connection_is_never_probed(monkeypatch):
    """Some setups answer on a path a bare TCP probe does not model.
    Breaking one of those to improve a message would be a bad trade, so
    the direct mode is left exactly as it always was."""
    from tgdatabridge.core.connector_factory import _resolve
    from tgdatabridge.db import access as access_mod

    def _boom(*_a, **_k):
        raise AssertionError("the direct path must not probe")

    monkeypatch.setattr(access_mod, "is_reachable", _boom)
    params = ConnectionParams("db.example.com", 5432, "app", "u", "p")
    assert _resolve(params) is params


def test_a_failed_direct_connection_is_told_about_the_private_options(monkeypatch):
    from tgdatabridge.db import access as access_mod

    monkeypatch.setattr(access_mod, "is_reachable", lambda *a, **k: False)
    hint = access_mod.direct_failure_hint("mydb.eu-west-1.rds.amazonaws.com", 5432)
    assert "SSH tunnel" in hint and "VPN" in hint
    monkeypatch.setattr(access_mod, "is_reachable", lambda *a, **k: True)
    assert access_mod.direct_failure_hint("db", 5432) is None


def test_the_chosen_mode_is_remembered(tmp_path):
    from tgdatabridge.utils import app_storage

    app_storage.save_connection_profile(
        "PostgreSQL", "10.0.3.44", 5432, "app", "u", None,
        base_dir=tmp_path, access_mode="vpn")
    (saved,) = app_storage.load_connection_profiles("PostgreSQL", base_dir=tmp_path)
    assert saved.access_mode == "vpn"
    assert "over VPN" in saved.display_label


def test_reopening_a_saved_connection_restores_the_choice(qt_app, tmp_path, monkeypatch):
    from tgdatabridge.utils import app_storage
    from tgdatabridge.db.access import ACCESS_SSH, ACCESS_VPN

    monkeypatch.setattr(app_storage, "app_data_dir", lambda base=None: tmp_path)
    app_storage.save_connection_profile(
        "MySQL", "10.0.3.44", 3306, "app", "u", None,
        base_dir=tmp_path, access_mode="vpn")
    app_storage.save_connection_profile(
        "MySQL", "10.0.3.45", 3306, "app", "u", None, base_dir=tmp_path,
        access_mode="ssh", ssh=_cfg())

    from tgdatabridge.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog("MySQL")
    by_label = {dialog.saved_combo.itemText(i): i
                for i in range(dialog.saved_combo.count())}
    vpn_index = next(i for label, i in by_label.items() if "over VPN" in label)
    dialog.saved_combo.setCurrentIndex(vpn_index)
    assert dialog.access_mode() == ACCESS_VPN

    ssh_index = next(i for label, i in by_label.items() if "via bastion" in label)
    dialog.saved_combo.setCurrentIndex(ssh_index)
    assert dialog.access_mode() == ACCESS_SSH
    assert dialog.ssh_host.text() == "bastion.example.com"


def test_the_cli_accepts_an_access_mode(monkeypatch, tmp_path):
    import json

    from tgdatabridge.cli import config as cli_config
    from tgdatabridge.db.access import ACCESS_VPN

    monkeypatch.setenv("SRC_PW", "dbpass")
    job = _job(None)
    job["source"].pop("ssh")
    job["source"]["access"] = "vpn"
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    parsed = cli_config.load_job_config(str(path))
    assert cli_config.resolve_connection_params(parsed.source).access_mode == ACCESS_VPN


def test_the_cli_rejects_a_contradictory_access_mode(tmp_path):
    import json

    from tgdatabridge.cli import config as cli_config

    job = _job({"host": "b", "username": "u", "private_key_path": __file__})
    job["source"]["access"] = "public"
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(cli_config.CliConfigError) as caught:
        cli_config.load_job_config(str(path))
    assert "ssh" in str(caught.value)


def test_a_non_standard_ssh_port_is_pointed_out(qt_app=None):
    """21 and 22 are one keystroke apart and produce the same
    "nothing answered" failure as a wrong address."""
    message = _explain(RuntimeError("Connection refused"), _cfg(port=21))
    assert "set to 21" in message and "22" in message
    assert "FTP" in message
    assert "set to 21" not in _explain(RuntimeError("Connection refused"), _cfg(port=22))


def test_the_mysql_connector_uses_the_pure_python_implementation():
    """The bundled C extension loads its auth plugins as separate DLLs
    from a directory that does not exist inside a frozen application, so
    the first real connection dies on

        2059 (HY000): Authentication plugin 'mysql_native_password'
        cannot be loaded: The specified module could not be found.

    -- which looks like a credentials problem and is not one.
    """
    import inspect

    from tgdatabridge.db import mysql_connector

    # use_pure=True now lives in _connect_kwargs() (extracted so the TLS/
    # SSL wiring around it is testable without a real driver installed --
    # see tests/test_mysql_connector.py's own TLS section), which
    # connect() calls; check both so this survives either shape.
    source = (inspect.getsource(mysql_connector.MySQLConnector.connect)
               + inspect.getsource(mysql_connector.MySQLConnector._connect_kwargs))
    assert "use_pure=True" in source
