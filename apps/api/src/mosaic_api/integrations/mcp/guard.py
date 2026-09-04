"""Admission rules for an operator-supplied MCP server URL.

Registering an MCP server is the first time MOSAIC makes an outbound call to a host it did not
derive from an Azure resource ID. MOSAIC runs with a managed identity, so an unconstrained URL is
a token-exfiltration surface, not merely a request-forgery one. These rules are applied before any
connection is attempted and again on every discovery run, so tightening them takes effect on
records registered before they existed.
"""

import ipaddress
from urllib.parse import urlsplit

from mosaic_api.errors import ValidationError

# The instance metadata service. Blocked explicitly as well as by the link-local range below,
# because it is the address whose reachability would matter most.
IMDS_ADDRESS = "169.254.169.254"


def _is_blocked_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A DNS name. Nothing is resolved here on purpose: resolving and then connecting would
        # check one answer and use another, so the check would prove nothing. The residual
        # rebinding exposure is stated in ADR 0007 rather than papered over.
        return False
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def admit_mcp_url(url: str, *, require_https: bool = True, allow_private: bool = False) -> str:
    """Return ``url`` unchanged if MOSAIC may connect to it, or raise :class:`ValidationError`."""

    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValidationError(
            "An MCP server URL must use http or https.",
            details={"scheme": parts.scheme},
        )
    if require_https and scheme != "https":
        raise ValidationError(
            "MCP servers must be registered over https.",
            details={"url": url},
        )
    host = parts.hostname
    if not host:
        raise ValidationError("An MCP server URL must include a host.", details={"url": url})
    if parts.username or parts.password:
        # Rejected rather than silently stripped. Stripping would store and echo the credential
        # while never actually sending it, which is the worst of both.
        raise ValidationError(
            "Do not put credentials in the MCP server URL. Register the server with a Key Vault "
            "secret or a managed-identity audience instead.",
            details={"host": host},
        )
    if allow_private:
        return url
    if host.casefold() in {"localhost", IMDS_ADDRESS} or _is_blocked_address(host):
        raise ValidationError(
            "MOSAIC will not connect to a loopback, link-local, or private address. It has no "
            "private network path to one, and refusing removes the risk of a managed-identity "
            "token reaching the instance metadata service.",
            details={"host": host},
        )
    return url
