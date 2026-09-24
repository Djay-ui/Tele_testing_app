"""Reaching a database that has no route from this machine.

A production database is normally not on the public internet. On AWS an
RDS instance usually sits in a private subnet: there is no public
endpoint to type into a connection dialog at all, and the only way in is
through something that *is* reachable -- a bastion / jump host in the
public subnet, or a VPN.

Those are two different problems and only one of them belongs in an
application:

* **VPN** (OpenVPN, AWS Client VPN, WireGuard, an IPsec tunnel) is an
  operating-system level route. Once the VPN is up, the private address
  resolves and connects like any other host, and this tool needs to know
  nothing about it -- type the private endpoint into Host and connect.
  There is no useful "VPN option" for an application to offer; a program
  cannot bring up a system route on the user's behalf without being that
  VPN client.
* **A bastion / jump host** is exactly what an application can do for
  itself, and is what this module implements. It opens an SSH connection
  to the jump host, asks it to forward a local port to the database's
  private address, and then the ordinary connector dials
  127.0.0.1:<local port> without knowing anything unusual happened.

  This is the same thing as running

      ssh -N -L 5433:mydb.abc123.eu-west-1.rds.amazonaws.com:5432 \\
          ec2-user@bastion.example.com

  in a terminal and pointing the tool at localhost:5433 -- except the
  tool starts it, watches it, reports its failures in the same dialog as
  every other connection failure, and shuts it down on exit.

Two backends, tried in this order, so a working tunnel does not depend on
anything being installed:

1. **asyncssh**, which is bundled with the packaged application. Pure
   Python on top of the `cryptography` library that ships with it, so
   there is nothing to install and no external process. Supports a
   private key (the usual AWS `.pem`), a password, or an agent.
2. **The system OpenSSH client** (`ssh`), used when asyncssh is not
   importable -- a source checkout without the dependency installed.
   Present by default on Windows 10/11, macOS and Linux. Key or agent
   authentication only: a password cannot be handed to `ssh` without a
   terminal.

Nothing here ever writes a key, a password or a passphrase to disk.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from tgdatabridge.utils import logger

DEFAULT_SSH_PORT = 22

AUTH_KEY = "Private key file"
AUTH_PASSWORD = "Password"
AUTH_AGENT = "SSH agent / default keys"
AUTH_METHODS = (AUTH_KEY, AUTH_PASSWORD, AUTH_AGENT)


@dataclass
class SshTunnelConfig:
    """Everything needed to reach a private database through a jump host.

    `remote_host` / `remote_port` are the address of the database *as the
    jump host sees it* -- normally the RDS endpoint, which resolves only
    inside the VPC. They are optional: left blank, the connection's own
    Host and Port are used, which is what someone who typed the private
    endpoint into the main form expects.
    """

    enabled: bool = False
    host: str = ""                      # the bastion / jump host
    port: int = DEFAULT_SSH_PORT
    username: str = ""
    auth_method: str = AUTH_KEY
    private_key_path: str = ""
    private_key_passphrase: str = ""
    password: str = ""
    remote_host: str = ""
    remote_port: int = 0
    # Off by default and deliberately so: a bastion's host key is not in
    # this machine's known_hosts the first time, and refusing to connect
    # with no way to accept the key from a dialog would make the feature
    # unusable. Tick it once the host is known and the tunnel will then
    # refuse to talk to anything that answers with a different key.
    verify_host_key: bool = False
    keepalive_seconds: int = 30

    def key(self) -> tuple:
        """Identity for reuse -- two connections through the same jump
        host to the same database share one tunnel."""
        return (self.host, self.port, self.username, self.auth_method,
                self.private_key_path, self.remote_host, self.remote_port,
                self.verify_host_key)

    def describe(self) -> str:
        who = f"{self.username}@" if self.username else ""
        return f"{who}{self.host}:{self.port}"

    def validate(self) -> Optional[str]:
        """A human-readable reason this configuration cannot work, or
        None. Checked before a connection is attempted so the user gets
        the real problem instead of a timeout."""
        if not self.enabled:
            return None
        if not self.host:
            return "Enter the SSH host (the bastion / jump server) to connect through."
        if not self.username:
            return "Enter the SSH username for the jump host (for Amazon Linux this is usually ec2-user; for Ubuntu, ubuntu)."
        if self.auth_method == AUTH_KEY:
            if not self.private_key_path:
                return "Choose the private key file (.pem) for the jump host."
            if not os.path.isfile(self.private_key_path):
                return f"Private key file not found: {self.private_key_path}"
        if self.auth_method == AUTH_PASSWORD and not self.password:
            return "Enter the SSH password for the jump host."
        if not (1 <= self.port <= 65535):
            return "The SSH port must be between 1 and 65535."
        return None


class SshTunnelError(RuntimeError):
    """Raised with a message meant to be shown to the user as-is."""


class _Tunnel:
    """Common surface: where to connect locally, and how to shut down."""

    local_host = "127.0.0.1"
    local_port = 0

    def close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def is_alive(self) -> bool:  # pragma: no cover - overridden
        return True


# --------------------------------------------------------------- asyncssh

class _AsyncSshTunnel(_Tunnel):
    """asyncssh, driven from a dedicated event loop on its own thread.

    A background thread rather than the caller's: the connectors are
    synchronous and are called from Qt worker threads, so there is no
    event loop to attach to and nothing may block the GUI thread.
    """

    def __init__(self, cfg: SshTunnelConfig, remote_host: str, remote_port: int):
        import asyncio

        import asyncssh

        self._asyncssh = asyncssh
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="databridge-ssh-tunnel", daemon=True)
        self._thread.start()
        self._conn = None
        self._listener = None

        connect_kwargs = {
            "host": cfg.host,
            "port": cfg.port,
            "username": cfg.username or None,
            # asyncssh's default is to *require* a known_hosts match. See
            # SshTunnelConfig.verify_host_key for why this is opt-in.
            "known_hosts": () if cfg.verify_host_key else None,
            "keepalive_interval": max(0, int(cfg.keepalive_seconds or 0)),
        }
        if cfg.auth_method == AUTH_KEY:
            connect_kwargs["client_keys"] = [cfg.private_key_path]
            if cfg.private_key_passphrase:
                connect_kwargs["passphrase"] = cfg.private_key_passphrase
        elif cfg.auth_method == AUTH_PASSWORD:
            connect_kwargs["password"] = cfg.password
            connect_kwargs["client_keys"] = None

        async def setup():
            conn = await asyncssh.connect(**connect_kwargs)
            listener = await conn.forward_local_port(
                self.local_host, 0, remote_host, remote_port)
            return conn, listener

        try:
            self._conn, self._listener = self._call(setup(), timeout=45)
        except Exception as exc:  # noqa: BLE001
            self._shutdown_loop()
            raise SshTunnelError(_explain(exc, cfg)) from exc
        self.local_port = self._listener.get_port()

    def _run_loop(self) -> None:
        self._loop.run_forever()

    def _call(self, coro, timeout: float):
        import asyncio

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def is_alive(self) -> bool:
        conn = self._conn
        return bool(conn is not None and not conn.is_closed()
                    and self._thread.is_alive())

    def close(self) -> None:
        try:
            if self._listener is not None:
                self._loop.call_soon_threadsafe(self._listener.close)
            if self._conn is not None:
                self._loop.call_soon_threadsafe(self._conn.abort)
        except Exception:  # noqa: BLE001
            pass
        self._shutdown_loop()

    def _shutdown_loop(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------- OpenSSH client

class _OpenSshTunnel(_Tunnel):
    """`ssh -N -L ...` as a child process, for environments where
    asyncssh is not installed."""

    def __init__(self, cfg: SshTunnelConfig, remote_host: str, remote_port: int):
        ssh = _find_ssh_binary()
        if ssh is None:
            raise SshTunnelError(
                "No SSH support is available: the bundled asyncssh library could not be "
                "imported and no `ssh` command was found on this machine.\n\n"
                "On Windows, install \"OpenSSH Client\" from Settings > Apps > Optional "
                "features, or run this tool from source with `pip install asyncssh`.")
        if cfg.auth_method == AUTH_PASSWORD:
            raise SshTunnelError(
                "Password authentication needs the bundled asyncssh library, which could "
                "not be imported here. The system `ssh` command cannot be given a password "
                "without a terminal.\n\nUse a private key file instead, or run from source "
                "with `pip install asyncssh`.")

        self.local_port = _free_local_port()
        command = [
            ssh, "-N", "-T",
            # Fail immediately -- and visibly -- if the forward cannot be
            # set up, instead of sitting there with a dead local port.
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-o", f"ServerAliveInterval={max(1, cfg.keepalive_seconds)}",
            "-o", "ConnectTimeout=20",
            "-p", str(cfg.port),
            "-L", f"{self.local_host}:{self.local_port}:{remote_host}:{remote_port}",
        ]
        if not cfg.verify_host_key:
            command += ["-o", "StrictHostKeyChecking=no",
                        "-o", f"UserKnownHostsFile={os.devnull}"]
        if cfg.auth_method == AUTH_KEY and cfg.private_key_path:
            command += ["-i", cfg.private_key_path, "-o", "IdentitiesOnly=yes"]
        command.append(f"{cfg.username}@{cfg.host}" if cfg.username else cfg.host)

        creation_flags = 0
        if sys.platform == "win32":  # keep a console window from flashing up
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=creation_flags)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                _out, err = self._proc.communicate()
                raise SshTunnelError(_explain_openssh(err, cfg))
            if _port_is_open(self.local_host, self.local_port):
                return
            time.sleep(0.2)
        self.close()
        raise SshTunnelError(
            f"Timed out setting up the SSH tunnel through {cfg.describe()}.")

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------- helpers

def _find_ssh_binary() -> Optional[str]:
    found = shutil.which("ssh")
    if found:
        return found
    if sys.platform == "win32":
        candidate = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_note(cfg: SshTunnelConfig) -> str:
    """SSH is port 22 almost everywhere. A non-standard value is
    occasionally deliberate and is usually a typo -- 21 (FTP) and 22 are
    one keystroke apart -- and it produces exactly the same "nothing
    answered" failure as a wrong address, so it is worth naming."""
    if cfg.port == 22:
        return ""
    known = {21: "FTP", 23: "Telnet", 25: "SMTP", 80: "HTTP", 443: "HTTPS",
             3306: "MySQL", 5432: "PostgreSQL", 1433: "SQL Server"}
    what = known.get(cfg.port)
    extra = f" -- {cfg.port} is the usual {what} port" if what else ""
    return (f"\n\nAlso check the SSH port: it is set to {cfg.port}, and SSH normally "
            f"listens on 22{extra}.")


def _explain(exc: BaseException, cfg: SshTunnelConfig) -> str:
    """Turn an asyncssh failure into something that says what to fix."""
    text = str(exc) or type(exc).__name__
    low = text.lower()
    where = cfg.describe()
    if "permission denied" in low or "authentication" in low:
        hint = ("the private key file" if cfg.auth_method == AUTH_KEY
                else "the password")
        return (f"The jump host {where} refused the login.\n\nCheck the username and "
                f"{hint}. On AWS the username is set by the AMI -- ec2-user for Amazon "
                f"Linux, ubuntu for Ubuntu, admin for Debian -- and is not the database "
                f"username.\n\n{text}")
    if "host key" in low or "known_hosts" in low:
        return (f"The host key of {where} is not trusted.\n\nUntick \"Verify host key\" "
                f"to accept it, or add the host to your known_hosts file first.\n\n{text}")
    if "bcrypt" in low:
        # asyncssh can decrypt a classic PEM key ("BEGIN RSA PRIVATE KEY"
        # with Proc-Type: 4,ENCRYPTED) using the cryptography library it
        # already has, but the newer OpenSSH container format encrypts its
        # key with the bcrypt KDF, which needs a compiled bcrypt module
        # this build does not ship. Say exactly that, and exactly how to
        # get past it, rather than leaking the library's own wording.
        return (f"The private key {cfg.private_key_path} is passphrase-protected in the "
                f"newer OpenSSH format, which this build cannot decrypt.\n\n"
                f"Either use the key without a passphrase (an AWS .pem downloaded from the "
                f"console has none), or make an unencrypted copy for this tool:\n\n"
                f"    ssh-keygen -p -f <copy-of-your-key>\n\n"
                f"and leave the new passphrase empty. A key in PEM format "
                f"(\"BEGIN RSA PRIVATE KEY\") works with its passphrase as-is.")
    if "passphrase" in low or "encrypted" in low or "decrypt" in low:
        return (f"The private key {cfg.private_key_path} could not be read.\n\nIf it is "
                f"passphrase-protected, enter the passphrase. Note that a key saved in "
                f"PuTTY's .ppk format is not an OpenSSH key -- export it as OpenSSH "
                f"first.\n\n{text}")
    if "timed out" in low or "timeout" in low:
        return (f"Timed out reaching the jump host {where}.\n\nCheck that its security "
                f"group allows SSH (port {cfg.port}) from this machine's public IP."
                + _port_note(cfg) + f"\n\n{text}")
    if "connection refused" in low or "unreachable" in low or "getaddrinfo" in low \
            or "name or service" in low or "resolve" in low:
        return (f"Could not reach the jump host {where}.\n\nCheck the address and port, "
                f"and that the host is running." + _port_note(cfg) + f"\n\n{text}")
    if "connect_timeout" in low or "open failed" in low or "administratively" in low:
        return (f"The jump host {where} accepted the login but refused to forward the "
                f"connection to the database.\n\nCheck that the database's security group "
                f"allows traffic from the jump host, and that the database endpoint and "
                f"port are right.\n\n{text}")
    return (f"Could not open the SSH tunnel through {where}."
            + _port_note(cfg) + f"\n\n{text}")


def _explain_openssh(stderr: str, cfg: SshTunnelConfig) -> str:
    text = (stderr or "").strip() or "the ssh command exited without a message"
    low = text.lower()
    where = cfg.describe()
    if "permission denied" in low:
        return (f"The jump host {where} refused the login.\n\nCheck the username and the "
                f"private key file.\n\n{text}")
    if "host key verification failed" in low:
        return (f"The host key of {where} is not trusted.\n\nUntick \"Verify host key\" "
                f"to accept it.\n\n{text}")
    return f"Could not open the SSH tunnel through {where}.\n\n{text}"


# --------------------------------------------------------------- manager

@dataclass
class _Entry:
    tunnel: _Tunnel
    backend: str
    description: str


class TunnelManager:
    """One tunnel per (jump host, database address), shared by every
    connector that needs it and kept open until the application closes.

    Connectors are short-lived -- each operation builds one, connects and
    closes it -- so tying a tunnel's lifetime to a connector would mean
    re-authenticating with the jump host for every click. Reuse also
    keeps the local port stable, which matters because the port is what
    the connection is addressed by.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: Dict[tuple, _Entry] = {}

    def ensure(self, cfg: SshTunnelConfig, host: str, port: int) -> Tuple[str, int]:
        """Local address to connect to instead of (host, port)."""
        problem = cfg.validate()
        if problem:
            raise SshTunnelError(problem)

        remote_host = cfg.remote_host.strip() or host
        remote_port = cfg.remote_port or port
        if not remote_host:
            raise SshTunnelError(
                "There is no database address to forward to. Enter the database's "
                "private endpoint in Host, or in the tunnel's own \"Database host\" field.")

        key = cfg.key() + (remote_host, remote_port)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.tunnel.is_alive():
                    return entry.tunnel.local_host, entry.tunnel.local_port
                # Went away (network drop, session timeout) -- rebuild it
                # rather than handing back a port nothing is listening on.
                logger.warning(
                    f"SSH tunnel through {cfg.describe()} had dropped; reopening it.")
                try:
                    entry.tunnel.close()
                except Exception:  # noqa: BLE001
                    pass
                self._entries.pop(key, None)

            tunnel, backend = _open(cfg, remote_host, remote_port)
            description = (f"127.0.0.1:{tunnel.local_port} -> {remote_host}:{remote_port} "
                           f"via {cfg.describe()}")
            self._entries[key] = _Entry(tunnel, backend, description)
            logger.info(
                f"SSH tunnel open ({backend}): 127.0.0.1:{tunnel.local_port} -> "
                f"{remote_host}:{remote_port} via {cfg.describe()}")
            return tunnel.local_host, tunnel.local_port

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            try:
                entry.tunnel.close()
            except Exception:  # noqa: BLE001
                pass
        if entries:
            logger.info(f"Closed {len(entries)} SSH tunnel(s).")

    def open_count(self) -> int:
        with self._lock:
            return len(self._entries)


def _open(cfg: SshTunnelConfig, remote_host: str, remote_port: int):
    """asyncssh if it imports, otherwise the system ssh client."""
    try:
        import asyncssh  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        logger.info(f"asyncssh unavailable ({exc}); falling back to the system ssh client.")
    else:
        return _AsyncSshTunnel(cfg, remote_host, remote_port), "asyncssh"
    return _OpenSshTunnel(cfg, remote_host, remote_port), "openssh"


#: Process-wide, because the tunnels are process-wide. main_window closes
#: this on exit; the CLI closes it when a run finishes.
tunnels = TunnelManager()


def resolve_params(params):
    """Return `params` addressed at the local end of a tunnel, opening one
    if the connection asks for it. Unchanged when it does not.

    This is the single place the rest of the tool goes through -- see
    connector_factory -- so every connector, the GUI's "Test Connection",
    and the headless CLI all get tunnelling for free and none of them
    needs to know it exists.
    """
    from dataclasses import replace

    cfg = getattr(params, "ssh", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        return params
    local_host, local_port = tunnels.ensure(cfg, params.host, params.port)
    return replace(params, host=local_host, port=local_port)
