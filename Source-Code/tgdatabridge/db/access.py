"""How this machine is supposed to reach the database.

Three answers, and the connection dialog asks for one of them outright
rather than leaving it implied:

* **Public / direct** -- the endpoint is reachable from here. This is what
  the tool has always done, and it stays the default, so an existing
  connection behaves exactly as it did.
* **Private, through an SSH tunnel** -- there is no route to the database,
  but there is one to a bastion / jump host that can reach it. The tool
  opens the port-forward itself; see tgdatabridge/db/ssh_tunnel.py.
* **Private, over a VPN** -- the route exists because a VPN client
  (OpenVPN, AWS Client VPN, WireGuard, an IPsec tunnel) has already put it
  there. Connecting is then identical to the public case: the private
  endpoint resolves, and the driver dials it directly.

The third mode connects the same way as the first, so it would be easy to
argue it should not exist. It earns its place for one reason: it says what
the user *expected*, which is what makes a failure explainable. "Nothing
is listening at 10.0.3.44:5432" is a shrug; "nothing is listening at
10.0.3.44:5432 -- the VPN this connection expects does not appear to be
up" is an instruction. A tool cannot bring up a VPN on someone's behalf --
that is an operating-system route, and only the VPN client can install it
-- but it can check the route is there and say so plainly when it is not.
"""
from __future__ import annotations

import socket
from typing import Optional

ACCESS_DIRECT = "Public — the database is reachable from this machine"
ACCESS_SSH = "Private — connect through an SSH tunnel (bastion / jump host)"
ACCESS_VPN = "Private — I am connected to a VPN that reaches it"
ACCESS_MODES = (ACCESS_DIRECT, ACCESS_SSH, ACCESS_VPN)

#: Short, stable tokens for the CLI and for saved profiles -- the labels
#: above are display text and must be free to change wording without
#: invalidating what is on disk.
MODE_TOKENS = {ACCESS_DIRECT: "public", ACCESS_SSH: "ssh", ACCESS_VPN: "vpn"}
MODE_FROM_TOKEN = {token: label for label, token in MODE_TOKENS.items()}


class NotReachableError(RuntimeError):
    """Raised with a message meant to be shown to the user as-is."""


def is_reachable(host: str, port: int, timeout: float = 6.0) -> bool:
    """Whether a TCP connection to host:port can be opened at all.

    Deliberately just the handshake -- no protocol, no credentials. A
    database driver's own failure for an unroutable address is a long
    timeout ending in a message about the driver, which tells the user
    nothing about the actual problem, so this answers the prior question
    first: is there a route?
    """
    if not host or not port:
        return True  # nothing to check -- a file engine, or an unset field
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def check_vpn_route(host: str, port: int, timeout: float = 6.0) -> None:
    """Raise if a connection declared as "over a VPN" has no route.

    Only for that mode. A direct connection is left to fail on its own
    terms -- some setups legitimately answer on a path a bare TCP probe
    does not model (a proxy, a Unix socket, a driver-side redirect), and
    breaking one of those to improve an error message would be a poor
    trade.
    """
    if is_reachable(host, port, timeout):
        return
    raise NotReachableError(
        f"{host}:{port} cannot be reached from this machine.\n\n"
        "This connection is set to \"Private — I am connected to a VPN\", so the "
        "address is expected to resolve through the VPN. Check that:\n\n"
        "  • the VPN client is connected right now (an expired session looks exactly "
        "like this),\n"
        "  • this endpoint is inside the network the VPN routes,\n"
        "  • the database's security group / firewall allows the VPN's address range.\n\n"
        "If there is no VPN and the database sits in a private subnet, switch this "
        "connection to \"Private — connect through an SSH tunnel\" and give it a jump "
        "host instead.")


def direct_failure_hint(host: str, port: int) -> Optional[str]:
    """Extra advice to append when a *direct* connection has failed and
    the address turns out not to be routable at all.

    Used by the connection dialog's Test Connection, where the driver's
    own message has already been shown and the useful next sentence is
    "this address is not reachable from here at all".
    """
    if is_reachable(host, port, timeout=4.0):
        return None
    return (f"\n\nSeparately: {host}:{port} does not accept a connection from this "
            f"machine at all, so this is a routing problem rather than a credentials "
            f"one.\n\nIf the database is in a private network -- an AWS RDS instance in "
            f"a private subnet, for example -- set \"How to reach it\" to \"Private — "
            f"connect through an SSH tunnel\" and enter your bastion / jump host, or "
            f"connect your VPN first and choose the VPN option.")
