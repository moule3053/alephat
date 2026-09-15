"""Network boundary checks for user-configured remote MCP endpoints."""

import asyncio
import ipaddress
from urllib.parse import urlparse


def _blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def validate_remote_url(url: str, *, allow_private: bool, resolve: bool = True) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MCP URL must use http or https and include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("MCP URL must not contain embedded credentials")
    if allow_private:
        return
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None and _blocked(literal):
        raise ValueError("MCP URL resolves to a private or reserved network")
    if not resolve:
        return
    loop = asyncio.get_running_loop()
    addresses = await loop.getaddrinfo(parsed.hostname, parsed.port or 443, type=0)
    for address in addresses:
        resolved = ipaddress.ip_address(address[4][0])
        if _blocked(resolved):
            raise ValueError("MCP URL resolves to a private or reserved network")
