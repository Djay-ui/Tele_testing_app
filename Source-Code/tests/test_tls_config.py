"""Tests for tgdatabridge.db.tls_config.TlsConfig -- the certificate-based
encryption settings shared by every connector's own TLS wiring (see each
connector's `_connect_kwargs`/`_conn_str`/`_dsn` tests for how each engine
actually uses these fields)."""
import ssl

import pytest

from tgdatabridge.db.tls_config import TlsConfig


def test_disabled_needs_no_validation():
    assert TlsConfig(enabled=False).validate() is None
    # Even with fields set that would otherwise be invalid -- turning TLS
    # off is always a safe, valid state regardless of what's left in the
    # certificate path boxes.
    assert TlsConfig(enabled=False, ca_cert_path="/does/not/exist").validate() is None


def test_a_default_enabled_config_is_valid():
    assert TlsConfig(enabled=True).validate() is None


def test_verifying_hostname_without_verifying_the_certificate_is_rejected():
    cfg = TlsConfig(enabled=True, verify_cert=False, verify_hostname=True)
    problem = cfg.validate()
    assert problem is not None
    assert "certificate" in problem.lower()


def test_a_missing_ca_cert_file_is_reported():
    cfg = TlsConfig(enabled=True, ca_cert_path="/no/such/file.pem")
    problem = cfg.validate()
    assert problem is not None
    assert "/no/such/file.pem" in problem


def test_ca_cert_path_is_found(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("not a real cert, just needs to exist", encoding="utf-8")
    assert TlsConfig(enabled=True, ca_cert_path=str(ca)).validate() is None


def test_a_client_cert_with_no_key_is_rejected(tmp_path):
    cert = tmp_path / "client.crt"
    cert.write_text("cert", encoding="utf-8")
    cfg = TlsConfig(enabled=True, client_cert_path=str(cert))
    problem = cfg.validate()
    assert problem is not None
    assert "matching private key" in problem


def test_a_client_key_with_no_cert_is_rejected(tmp_path):
    key = tmp_path / "client.key"
    key.write_text("key", encoding="utf-8")
    cfg = TlsConfig(enabled=True, client_key_path=str(key))
    problem = cfg.validate()
    assert problem is not None
    assert "matching private key" in problem


def test_a_matched_client_cert_and_key_are_valid(tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    cfg = TlsConfig(enabled=True, client_cert_path=str(cert), client_key_path=str(key))
    assert cfg.validate() is None


def test_a_missing_client_cert_file_is_reported(tmp_path):
    key = tmp_path / "client.key"
    key.write_text("key", encoding="utf-8")
    cfg = TlsConfig(enabled=True, client_cert_path=str(tmp_path / "nope.crt"),
                     client_key_path=str(key))
    problem = cfg.validate()
    assert problem is not None
    assert "Client certificate" in problem


def test_describe_names_the_verification_level():
    assert TlsConfig(enabled=False).describe() == "not encrypted"
    assert "not verified" in TlsConfig(enabled=True, verify_cert=False).describe()
    assert "hostname not checked" in TlsConfig(
        enabled=True, verify_cert=True, verify_hostname=False).describe()
    full = TlsConfig(enabled=True).describe()
    assert "certificate and hostname verified" in full
    assert "client certificate" not in full
    mutual = TlsConfig(
        enabled=True, client_cert_path="/x.crt", client_key_path="/x.key").describe()
    assert "client certificate" in mutual


def test_effective_hostname_falls_back_to_the_connection_host():
    cfg = TlsConfig(enabled=True)
    assert cfg.effective_hostname("db.example.com") == "db.example.com"


def test_effective_hostname_prefers_the_override():
    cfg = TlsConfig(enabled=True, server_host_override="real-db.internal")
    assert cfg.effective_hostname("127.0.0.1") == "real-db.internal"


def test_build_ssl_context_defaults_to_full_verification():
    ctx = TlsConfig(enabled=True).build_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_build_ssl_context_with_verification_off():
    ctx = TlsConfig(enabled=True, verify_cert=False).build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_build_ssl_context_with_only_hostname_check_off():
    ctx = TlsConfig(enabled=True, verify_cert=True, verify_hostname=False).build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is False


def test_build_ssl_context_loads_a_ca_file(tmp_path):
    # A real PEM isn't needed to prove the path was passed through --
    # ssl.create_default_context(cafile=...) is what would raise if the
    # path were wrong, and that's exercised by the "not found" validate()
    # tests above; this just confirms no exception on a well-formed
    # (if minimal) file is not accidentally swallowed here for a *good* CA.
    import ssl as _ssl

    ca_path = _make_self_signed_ca(tmp_path)
    ctx = TlsConfig(enabled=True, ca_cert_path=str(ca_path)).build_ssl_context()
    assert isinstance(ctx, _ssl.SSLContext)


def _make_self_signed_ca(tmp_path):
    """A minimal real self-signed certificate, so build_ssl_context's
    ssl.create_default_context(cafile=...) has something genuine to parse
    rather than failing on garbage text -- needed only for the one test
    above that loads a CA file end to end."""
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        pytest.skip("the cryptography library is not installed")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "ca.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path
