"""Tests for the headless CLI's "tls" config block -- tgdatabridge/cli/config.py's
_parse_tls/_tls_params, the CLI-config counterpart of the connection
dialog's "Security (TLS/SSL)" group (see tests/test_connection_dialog_tls.py)
and of the existing "ssh" block (see tests/test_ssh_tunnel.py's own CLI
config section, which this mirrors)."""
import json

import pytest

from tgdatabridge.cli import config as cli_config


def _job(tls_block=None, source_overrides=None):
    source = {
        "engine": "MySQL", "host": "db.internal", "port": 3306,
        "database": "app", "username": "u", "password_env": "SRC_PW",
    }
    if tls_block is not None:
        source["tls"] = tls_block
    if source_overrides:
        source.update(source_overrides)
    return {
        "source": source,
        "target": {"engine": "PostgreSQL", "host": "t", "port": 5432,
                   "database": "app", "username": "u", "password_env": "TGT_PW"},
    }


def _load(tmp_path, job_dict):
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job_dict), encoding="utf-8")
    return cli_config.load_job_config(str(path))


def test_no_tls_block_means_unencrypted(monkeypatch, tmp_path):
    monkeypatch.setenv("SRC_PW", "dbpass")
    monkeypatch.setenv("TGT_PW", "dbpass")
    job = _load(tmp_path, _job())
    params = cli_config.resolve_connection_params(job.source)
    assert params.tls is None


def test_an_empty_tls_block_enables_encryption_with_secure_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("SRC_PW", "dbpass")
    monkeypatch.setenv("TGT_PW", "dbpass")
    job = _load(tmp_path, _job(tls_block={}))
    params = cli_config.resolve_connection_params(job.source)
    assert params.tls.enabled is True
    assert params.tls.verify_cert is True
    assert params.tls.verify_hostname is True


def test_tls_block_reads_certificate_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("SRC_PW", "dbpass")
    monkeypatch.setenv("TGT_PW", "dbpass")
    job = _load(tmp_path, _job(tls_block={
        "ca_cert_path": "/ca.pem",
        "client_cert_path": "/client.crt",
        "client_key_path": "/client.key",
    }))
    params = cli_config.resolve_connection_params(job.source)
    assert params.tls.ca_cert_path == "/ca.pem"
    assert params.tls.client_cert_path == "/client.crt"
    assert params.tls.client_key_path == "/client.key"


def test_tls_block_resolves_client_key_password_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SRC_PW", "dbpass")
    monkeypatch.setenv("TGT_PW", "dbpass")
    monkeypatch.setenv("CLIENT_KEY_PASS", "s3cret")
    job = _load(tmp_path, _job(tls_block={
        "client_cert_path": "/c.crt", "client_key_path": "/c.key",
        "client_key_password_env": "CLIENT_KEY_PASS",
    }))
    params = cli_config.resolve_connection_params(job.source)
    assert params.tls.client_key_password == "s3cret"


def test_tls_block_never_takes_a_secret_from_the_file_itself(tmp_path):
    with pytest.raises(cli_config.CliConfigError, match="unknown field"):
        _load(tmp_path, _job(tls_block={"client_key_password": "hunter2"}))


def test_missing_client_key_password_env_var_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("SRC_PW", "dbpass")
    monkeypatch.setenv("TGT_PW", "dbpass")
    job = _load(tmp_path, _job(tls_block={
        "client_cert_path": "/c.crt", "client_key_path": "/c.key",
        "client_key_password_env": "NOT_SET_ANYWHERE",
    }))
    with pytest.raises(cli_config.CliConfigError, match="NOT_SET_ANYWHERE"):
        cli_config.resolve_connection_params(job.source)


def test_tls_verify_cert_must_be_a_boolean(tmp_path):
    with pytest.raises(cli_config.CliConfigError, match="verify_cert"):
        _load(tmp_path, _job(tls_block={"verify_cert": "yes"}))


def test_tls_hostname_verification_requires_cert_verification(tmp_path):
    with pytest.raises(cli_config.CliConfigError, match="verify_cert"):
        _load(tmp_path, _job(tls_block={"verify_cert": False, "verify_hostname": True}))


def test_tls_client_cert_needs_its_key(tmp_path):
    with pytest.raises(cli_config.CliConfigError, match="mutual TLS"):
        _load(tmp_path, _job(tls_block={"client_cert_path": "/c.crt"}))


def test_tls_client_key_needs_its_cert(tmp_path):
    with pytest.raises(cli_config.CliConfigError, match="mutual TLS"):
        _load(tmp_path, _job(tls_block={"client_key_path": "/c.key"}))


def test_tls_verify_cert_off_still_encrypts(monkeypatch, tmp_path):
    monkeypatch.setenv("SRC_PW", "dbpass")
    monkeypatch.setenv("TGT_PW", "dbpass")
    job = _load(tmp_path, _job(tls_block={"verify_cert": False, "verify_hostname": False}))
    params = cli_config.resolve_connection_params(job.source)
    assert params.tls.enabled is True
    assert params.tls.verify_cert is False
    assert params.tls.verify_hostname is False


def test_tls_composes_with_an_ssh_tunnel(monkeypatch, tmp_path):
    """The two are independent knobs -- a job can ask for both at once."""
    monkeypatch.setenv("SRC_PW", "dbpass")
    monkeypatch.setenv("TGT_PW", "dbpass")
    monkeypatch.setenv("BASTION_KEY_PASS", "keypass")
    job = _load(tmp_path, _job(
        tls_block={"ca_cert_path": "/ca.pem"},
        source_overrides={"ssh": {
            "host": "bastion.example.com", "username": "ec2-user",
            "private_key_path": __file__, "passphrase_env": "BASTION_KEY_PASS",
        }},
    ))
    params = cli_config.resolve_connection_params(job.source)
    assert params.tls.enabled is True
    assert params.tls.ca_cert_path == "/ca.pem"
    assert params.ssh.enabled is True
    assert params.ssh.host == "bastion.example.com"


def test_a_file_source_engine_has_no_tls_block(tmp_path):
    job_dict = {
        "source": {"engine": "Excel/CSV", "database": "/data/sales.xlsx"},
        "target": {"engine": "PostgreSQL", "host": "t", "port": 5432,
                   "database": "app", "username": "u", "password_env": "TGT_PW"},
    }
    job = _load(tmp_path, job_dict)
    assert job.source.tls is None
