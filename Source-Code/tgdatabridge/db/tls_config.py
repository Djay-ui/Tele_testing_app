"""TLS/SSL (certificate-based encryption) for a database connection --
Encrypt in Transit, in the vocabulary an enterprise security review uses.

This is a separate concern from tgdatabridge/db/ssh_tunnel.py's bastion
support, and the two compose: a connection can be tunneled, encrypted,
both, or neither. Nothing here opens a socket -- this module only builds
the configuration each connector's own driver needs to negotiate TLS on
the socket *it* opens, in whatever shape that particular driver wants it
(a ready ssl.SSLContext for python-oracledb, plain file paths for
mysql-connector-python and psycopg, ODBC connection-string keywords for
SQL Server, and so on) -- see each connector's own `connect()`.

Why hostname verification gets its own field, separate from certificate
verification
-----------------------------------------------------------------------
A certificate can be validated two different ways, and only one of them
needs a name to check:

  1. Is it signed by a CA this machine trusts? (verify_cert)
  2. Does it say it's *for this server* -- does its CN/SAN match the
     address being connected to? (verify_hostname)

Enterprise-grade TLS wants both. But an SSH-tunneled connection dials
127.0.0.1 (see connector_factory._resolve) -- the certificate is for the
real database endpoint, not localhost, so a naive hostname check would
fail every tunneled connection stone dead. `server_host_override` is the
fix: connector_factory._pin_tls_hostname captures the real address before
the tunnel rewrites `host`, and each connector checks the certificate's
name against that instead of against 127.0.0.1. Not every driver used
here exposes a way to give the connection address and the verification
name separately -- see each connector's own TLS section for how (or
whether) it manages this.
"""
from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from typing import Optional


@dataclass
class TlsConfig:
    """Certificate-based encryption settings for one connection.

    Only file *paths* and on/off switches live here -- never a private
    key's own bytes and never a passphrase. `client_key_password`
    (needed to unlock an encrypted client private key, if one is used
    for mutual TLS) is the one secret-shaped field, and it is never
    persisted -- see tgdatabridge/utils/app_storage.py's ConnectionProfile,
    which mirrors this exactly for the SSH tunnel's own key passphrase.
    """

    enabled: bool = False

    # Trust a CA the OS doesn't already know about (a private/enterprise
    # CA is the normal case for an internal database). Blank means "use
    # the operating system's own trust store" -- the right default for a
    # public cloud database with a certificate from a public CA.
    ca_cert_path: str = ""

    # Verify the server's certificate against a trusted CA at all. Off
    # is still "encrypted" (TLS is negotiated either way) but not
    # authenticated -- opportunistic encryption, safe against a passive
    # eavesdropper but not against an active man-in-the-middle. Kept as
    # an explicit opt-out rather than removed outright because it is a
    # real, named mode every driver here supports and some self-signed
    # internal test databases have no CA to verify against at all.
    verify_cert: bool = True

    # Verify that the certificate's CN/SAN actually names the server
    # being connected to. Requires verify_cert (see validate()) -- a
    # hostname match on a certificate nobody checked the signer of proves
    # nothing.
    verify_hostname: bool = True

    # Mutual TLS: this machine's own certificate and private key,
    # presented to the server. Both or neither -- a certificate with no
    # key cannot be used, and a key with no certificate has nothing to
    # attach it to. Not every engine here supports client certificates
    # over its wire protocol (SQL Server's ODBC driver notably does not
    # expose one via connection string); see each connector's own notes.
    client_cert_path: str = ""
    client_key_path: str = ""
    client_key_password: str = ""

    # Set by connector_factory._pin_tls_hostname, not by the connection
    # dialog: the real database address, captured before an SSH tunnel
    # (if any) rewrites ConnectionParams.host to 127.0.0.1. Blank means
    # "not tunneled, or nothing to override" -- verify against
    # ConnectionParams.host as normal.
    server_host_override: str = ""

    def validate(self) -> Optional[str]:
        """A human-readable reason this configuration cannot work, or
        None. Checked before a connection is attempted, the same
        contract as SshTunnelConfig.validate()."""
        if not self.enabled:
            return None
        if self.verify_hostname and not self.verify_cert:
            return ("Verifying the hostname requires verifying the certificate too -- "
                    "turn on \"Verify server certificate\", or turn off hostname "
                    "verification as well.")
        if self.ca_cert_path and not os.path.isfile(self.ca_cert_path):
            return f"CA certificate file not found: {self.ca_cert_path}"
        if bool(self.client_cert_path) != bool(self.client_key_path):
            return ("A client certificate needs its matching private key (and a private "
                    "key needs its certificate) for mutual TLS -- only one of the two "
                    "was given.")
        if self.client_cert_path and not os.path.isfile(self.client_cert_path):
            return f"Client certificate file not found: {self.client_cert_path}"
        if self.client_key_path and not os.path.isfile(self.client_key_path):
            return f"Client private key file not found: {self.client_key_path}"
        return None

    def describe(self) -> str:
        """One line for a log or a status message."""
        if not self.enabled:
            return "not encrypted"
        if not self.verify_cert:
            return "TLS/SSL (encrypted, certificate not verified)"
        if not self.verify_hostname:
            return "TLS/SSL (certificate verified, hostname not checked)"
        mutual = " + client certificate" if self.client_cert_path else ""
        return f"TLS/SSL (certificate and hostname verified{mutual})"

    def effective_hostname(self, connection_host: str) -> str:
        """The name to check a certificate against: the real database
        address if this connection is tunneled (see
        `server_host_override`'s own docstring), otherwise whatever host
        the connector was actually given."""
        return self.server_host_override or connection_host

    def build_ssl_context(self) -> ssl.SSLContext:
        """A ready `ssl.SSLContext`, for the one driver here that takes
        one directly (python-oracledb's thin mode -- see
        oracle_connector.py). Every other connector's driver wants plain
        paths/flags of its own shape instead and builds those itself
        from this object's fields directly, without going through
        `ssl` at all.
        """
        ctx = ssl.create_default_context(cafile=self.ca_cert_path or None)
        if not self.verify_cert:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif not self.verify_hostname:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_REQUIRED
        if self.client_cert_path:
            ctx.load_cert_chain(
                self.client_cert_path, self.client_key_path or None,
                self.client_key_password or None)
        return ctx


__all__ = ["TlsConfig"]
